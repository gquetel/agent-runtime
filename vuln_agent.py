#!/usr/bin/env python3
"""Autonomous vulnerability-research agent: orchestration CLI.

  vuln-agent run [options]         Run the Operating Loop for one systemd-
                                    managed session (a "nightly" or "manual"
                                    instance of vuln-agent@<mode>.service in
                                    nixconfigs). Each iteration is one pass of
                                    the loop defined in CLAUDE.md; the loop
                                    continues until a stop condition is hit:
                                    the configured cutoff, a usage-limit
                                    signal from the stream, or SIGTERM/SIGINT.

  vuln-agent metrics <status.json> <output.prom>
                                    Convert a status.json snapshot (written by
                                    `run` on every state change) into a
                                    node-exporter textfile-collector metric.
                                    Kept in this module, not a separate
                                    script, so the metric and the schema it
                                    reads never drift apart.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_PROMPT = (
    "Read CLAUDE.md. Pull the active work item and its comments from Plane, "
    "run exactly one iteration of the Operating Loop, then stop."
)

# Fixed contracts, not knobs: the executable name never varies, and these
# paths are the mount-layout convention shared with the guest poller (which
# writes PROMPT_FILE) and the `metrics` subcommand (which reads STATUS_FILE).
CLAUDE_BIN = "claude"
PROMPT_FILE = Path("/work/state/manual.prompt")
STATUS_FILE = Path("/work/state/status.json")

# Reactive detection patterns, checked against every streamed line.
USAGE_LIMIT_PATTERNS = [
    re.compile(r"hit your (session|usage) limit", re.I),
    re.compile(r"usage limit reached", re.I),
    re.compile(r"rate_limit_error", re.I),
]
SERVER_ERROR_PATTERNS = [
    re.compile(r"\b5\d\d\b.*(error|bad gateway|gateway timeout)", re.I),
    re.compile(r"error 5\d\d", re.I),
    re.compile(r"retry_after", re.I),
    re.compile(r"overloaded_error", re.I),
]


def matches_any(line: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(line) for p in patterns)


def local_now() -> datetime:
    return datetime.now().astimezone()


def log(msg: str) -> None:
    """Print a shim-originated status line, prefixed with the current time.

    Reserved for our own control-flow messages, not for lines relayed from
    the Claude invocation's stdout (raw passthrough or formatted stream
    events), which carry their own provenance and shouldn't look shim-timed.
    """
    print(f"[{local_now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _hhmm(s: str) -> tuple[int, int]:
    hh, mm = (int(x) for x in s.split(":"))
    return hh, mm


# --------------------------------------------------------------------------- #
# `run`
# --------------------------------------------------------------------------- #


@dataclass
class RunConfig:
    mode: str  # "nightly" | "manual"
    night_start: str = "23:00"
    cutoff: str = "04:00"
    manual_min: int = 60
    wait_server_err: int = 120
    wait_clean: int = 60
    model: str = "sonnet"


class Runner:
    """One systemd-managed session: a bounded sequence of Claude invocations.

    `mode=nightly` runs inside the [night_start, cutoff) window (crossing
    midnight); `mode=manual` is wall-clock boxed to `manual_min` minutes from
    start instead. Exposed as instance state (not module globals) so the
    signal handler and the main loop share one object without relying on
    import-time process state.
    """

    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.stop_at = 0.0
        self.resume_prompt = DEFAULT_PROMPT
        self.child: subprocess.Popen | None = None
        self.stop_requested = False

    # --- clock window / stop instant --------------------------------------- #

    def in_night_window(self, now: datetime | None = None) -> bool:
        """True if `now` is inside [night_start, cutoff), a window that
        crosses midnight (e.g. 23:00 <= now, or now < 04:00)."""
        now = now or local_now()
        s_h, s_m = _hhmm(self.cfg.night_start)
        e_h, e_m = _hhmm(self.cfg.cutoff)
        start = now.replace(hour=s_h, minute=s_m, second=0, microsecond=0)
        end = now.replace(hour=e_h, minute=e_m, second=0, microsecond=0)
        return now >= start or now < end

    def next_cutoff(self) -> datetime:
        """Next occurrence of `cutoff` (today if still ahead, else tomorrow).
        From a 23:00 start this lands on tomorrow 04:00, the full 5h window."""
        e_h, e_m = _hhmm(self.cfg.cutoff)
        end = local_now().replace(hour=e_h, minute=e_m, second=0, microsecond=0)
        if local_now() >= end:
            end += timedelta(days=1)
        return end

    def compute_stop_at(self) -> float:
        if self.cfg.mode == "manual":
            return time.time() + self.cfg.manual_min * 60
        return self.next_cutoff().timestamp()

    def load_manual_prompt(self) -> None:
        """Adopt the operator's custom resume-prompt for a manual run, if any.
        Read-only: the guest poller owns this file's lifecycle (writes it
        before starting us; we never write to it)."""
        try:
            txt = PROMPT_FILE.read_text().strip()
        except (FileNotFoundError, PermissionError):
            txt = ""
        if txt:
            self.resume_prompt = txt
            log("[request] manual run with custom prompt")
        else:
            log("[request] manual run with default prompt")

    def preflight(self) -> tuple[bool, str]:
        """Return (ok_to_run, reason). ok_to_run=False means stop the session."""
        if self.cfg.mode == "nightly" and not self.in_night_window():
            return False, "outside night window"
        if time.time() >= self.stop_at:
            return False, "stop instant reached"
        return True, "ok"

    # --- operator status snapshot ------------------------------------------ #

    def write_status(self, state: str, **fields) -> None:
        """Atomically overwrite status_file with the current status snapshot.

        Best-effort: the host CLI and the `metrics` subcommand read this
        file, but its absence or a transient write failure must never affect
        the run itself.
        """
        payload = {
            "mode": self.cfg.mode,
            "state": state,  # "running" | "idle"
            "updated_at": local_now().isoformat(),
            **fields,
        }
        try:
            tmp = STATUS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(STATUS_FILE)  # atomic on the same filesystem
        except OSError as exc:
            log(f"[status] failed to write {STATUS_FILE}: {exc!r}")

    # --- docker cleanup between iterations ---------------------------------- #
    # (belt-and-suspenders; CLAUDE.md also prunes)

    def docker_cleanup(self) -> None:
        try:
            ids = subprocess.run(
                ["docker", "ps", "-q"], capture_output=True, text=True, timeout=30
            ).stdout.split()
            for cid in ids:
                subprocess.run(["docker", "stop", cid], capture_output=True, timeout=60)
                subprocess.run(["docker", "rm", cid], capture_output=True, timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # docker is a fallback path; absence/hang is non-fatal

    # --- stream formatting --------------------------------------------------- #

    @staticmethod
    def _truncate(s: str, n: int = 120) -> str:
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[: n - 1] + "…"

    @staticmethod
    def _extract_model(evt: dict) -> str | None:
        """Ground-truth model id as reported by Claude's own stream output.

        Read from the stream (not our --model arg) so the log reflects what
        actually served the turn: the `system`/init event announces the
        resolved model, and each assistant message carries the model the API
        returned.
        """
        if evt.get("type") == "system" and evt.get("model"):
            return evt["model"]
        if evt.get("type") == "assistant":
            return evt.get("message", {}).get("model")
        return None

    def _format_stream_line(self, evt: dict) -> str | None:
        """Pretty one-liner for a stream-json event, or None to skip."""
        t = evt.get("type")
        if t == "assistant":
            parts = []
            for block in evt.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    parts.append(self._truncate(block["text"]))
                elif block.get("type") == "tool_use":
                    arg = ""
                    inp = block.get("input", {})
                    if isinstance(inp, dict) and inp:
                        first = next(iter(inp.values()))
                        arg = self._truncate(first)
                    parts.append(f"→ {block.get('name')}({arg})")
            return "  ".join(p for p in parts if p) or None
        if t == "user":
            for block in evt.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, list):
                        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                    return f"  ⤶ {self._truncate(c)}"
        if t == "result":
            return f"[result] {self._truncate(evt.get('result', ''), 200)}"
        return None

    # --- one Claude invocation ------------------------------------------------ #

    def run_claude(self) -> str:
        """Run one iteration. Returns an exit reason:
        'usage_limit' | 'server_error' | 'clean' | 'cutoff'.

        The iteration is killed at stop_at, so no message is sent past it.
        """
        cmd = [
            CLAUDE_BIN, "--print",
            "--model", self.cfg.model,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            self.resume_prompt,
        ]
        log("=== iteration start ===")
        self.child = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )

        reason = "clean"
        model_logged = False

        try:
            for line in self.child.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                if not line:
                    continue

                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    if matches_any(line, USAGE_LIMIT_PATTERNS):
                        reason = "usage_limit"
                        self.kill_child()
                        break
                    if matches_any(line, SERVER_ERROR_PATTERNS):
                        reason = "server_error"
                        log(f"[reactive] server_error line: {self._truncate(line, 200)}")
                        self.kill_child()
                        break
                    print(line, flush=True)
                    continue

                # Usage/server-limit signals also arrive as structured events
                # (an assistant text block, then a result event) — not just
                # raw non-JSON lines — so scan those too, else we sail past a
                # session-limit stop and loop straight back into the same
                # wall. Server-error matching is scoped to failed result
                # events so a PoC merely discussing HTTP 5xx doesn't trip it.
                et = evt.get("type")
                if et == "assistant":
                    txt = " ".join(
                        b.get("text", "")
                        for b in evt.get("message", {}).get("content", [])
                        if b.get("type") == "text"
                    )
                    if matches_any(txt, USAGE_LIMIT_PATTERNS):
                        reason = "usage_limit"
                        self.kill_child()
                        break
                elif et == "result":
                    txt = str(evt.get("result", ""))
                    if matches_any(txt, USAGE_LIMIT_PATTERNS):
                        reason = "usage_limit"
                        self.kill_child()
                        break
                    if evt.get("is_error") and matches_any(txt, SERVER_ERROR_PATTERNS):
                        reason = "server_error"
                        log(f"[reactive] server_error result: {self._truncate(txt, 200)}")
                        self.kill_child()
                        break

                if not model_logged:
                    model = self._extract_model(evt)
                    if model:
                        log(f"[model] serving turn with {model} (requested: {self.cfg.model})")
                        model_logged = True

                pretty = self._format_stream_line(evt)
                if pretty:
                    print(pretty, flush=True)

                if time.time() >= self.stop_at:
                    reason = "cutoff"
                    self.kill_child()
                    break
        finally:
            if self.child:
                self.child.wait()

        return reason

    def kill_child(self) -> None:
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.child.kill()

    # --- signal handling / main loop ---------------------------------------- #

    def on_term(self, signum, frame) -> None:
        self.stop_requested = True
        self.kill_child()

    def main_loop(self) -> int:
        if self.cfg.mode == "manual":
            self.load_manual_prompt()
        self.stop_at = self.compute_stop_at()
        started_at = local_now().isoformat()
        stop_at_iso = datetime.fromtimestamp(self.stop_at).astimezone().isoformat()
        log(f"[shim] mode={self.cfg.mode} stop_at={stop_at_iso}")

        exit_reason = "stopped"  # overwritten on every path out of the loop below
        try:
            self.write_status("running", started_at=started_at, stop_at=stop_at_iso)

            while not self.stop_requested:
                ok, why = self.preflight()
                if not ok:
                    log(f"[preflight] stopping: {why}")
                    exit_reason = why
                    break

                self.docker_cleanup()
                self.write_status(
                    "running", started_at=started_at, stop_at=stop_at_iso,
                    last_heartbeat=local_now().isoformat(),
                )
                reason = self.run_claude()

                if self.stop_requested:
                    exit_reason = "stopped"
                    break
                if reason == "usage_limit":
                    log("[reactive] session/usage limit hit — stopping")
                    exit_reason = reason
                    break
                if reason == "cutoff":
                    log("[reactive] stop instant reached — stopping")
                    exit_reason = reason
                    break
                if reason == "server_error":
                    log(f"[reactive] server error — backing off {self.cfg.wait_server_err}s")
                    time.sleep(self.cfg.wait_server_err)
                else:
                    time.sleep(self.cfg.wait_clean)
        except Exception as exc:
            exit_reason = f"crash: {exc!r}"
            log(f"[shim] unhandled exception: {exc!r}")
            raise
        finally:
            self.docker_cleanup()
            self.write_status("idle", last_exit_reason=exit_reason,
                               last_exit_at=local_now().isoformat())
            log("[shim] exiting")

        return 0


# A signal handler must be a plain function; it forwards to whichever Runner
# `cmd_run` made active for this process (there is ever only one per process).
_active_runner: Runner | None = None


def _signal_handler(signum, frame) -> None:
    if _active_runner is not None:
        _active_runner.on_term(signum, frame)


def cmd_run(args: argparse.Namespace) -> int:
    global _active_runner
    cfg = RunConfig(
        mode=args.mode,
        night_start=args.night_start,
        cutoff=args.cutoff,
        manual_min=args.manual_min,
        wait_server_err=args.wait_server_err,
        wait_clean=args.wait_clean,
        model=args.model,
    )
    _active_runner = Runner(cfg)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    return _active_runner.main_loop()


# --------------------------------------------------------------------------- #
# `metrics`
# --------------------------------------------------------------------------- #


def _escape_label(s: str) -> str:
    """Escape a string for use as a Prometheus label value."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def cmd_metrics(args: argparse.Namespace) -> int:
    """Convert a status.json snapshot into a node-exporter textfile metric.

    Run by the host-side `vuln-agent-metrics` timer in nixconfigs, which
    mounts the guest's shared state dir and has no other visibility into the
    agent's run state.
    """
    status_path: Path = args.status_json
    out_path: Path = args.output_prom

    if not status_path.exists():
        out_path.unlink(missing_ok=True)
        return 0

    status = json.loads(status_path.read_text())
    mode = _escape_label(str(status.get("mode", "unknown")))
    running = 1 if status.get("state") == "running" else 0
    reason = _escape_label(str(status.get("last_exit_reason") or "none")[:200])
    updated_at = status.get("updated_at")
    age = time.time() - datetime.fromisoformat(updated_at).timestamp() if updated_at else -1

    lines = [
        "# HELP vuln_agent_running Whether the vuln-research agent session is running (1) or idle (0).",
        "# TYPE vuln_agent_running gauge",
        f'vuln_agent_running{{mode="{mode}"}} {running}',
        "# HELP vuln_agent_status_age_seconds Age in seconds of the last status snapshot from the guest.",
        "# TYPE vuln_agent_status_age_seconds gauge",
        f"vuln_agent_status_age_seconds {age:.0f}",
        "# HELP vuln_agent_last_exit_info Last exit reason, as a label; value is always 1.",
        "# TYPE vuln_agent_last_exit_info gauge",
        f'vuln_agent_last_exit_info{{reason="{reason}"}} 1',
        "",
    ]

    tmp = out_path.with_suffix(".prom.tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(out_path)  # atomic on the same filesystem
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vuln-agent",
        description="Autonomous vulnerability-research agent: orchestration CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run", help="Run the Operating Loop for one systemd-managed session."
    )
    p_run.add_argument(
        "--mode", required=True, choices=["nightly", "manual"],
        help="nightly: bound to the night window; manual: bound to --manual-min.",
    )
    p_run.add_argument(
        "--night-start", default="23:00", metavar="HH:MM",
        help="Start of the nightly window (default: %(default)s).",
    )
    p_run.add_argument(
        "--cutoff", default="04:00", metavar="HH:MM",
        help="End of the nightly window, next day (default: %(default)s).",
    )
    p_run.add_argument(
        "--manual-min", type=int, default=60, metavar="MINUTES",
        help="Wall-clock box for a manual run (default: %(default)s).",
    )
    p_run.add_argument(
        "--wait-server-err", type=int, default=120, metavar="SECONDS",
        help="Backoff after a server-error iteration (default: %(default)s).",
    )
    p_run.add_argument(
        "--wait-clean", type=int, default=60, metavar="SECONDS",
        help="Delay between iterations otherwise (default: %(default)s).",
    )
    p_run.add_argument(
        "--model", default="sonnet", help="Model to request (default: %(default)s).",
    )
    p_run.set_defaults(func=cmd_run)

    p_metrics = sub.add_parser(
        "metrics", help="Convert a status.json snapshot into a node-exporter textfile metric."
    )
    p_metrics.add_argument("status_json", type=Path, help="Path to the status.json to read.")
    p_metrics.add_argument("output_prom", type=Path, help="Path to the .prom file to write.")
    p_metrics.set_defaults(func=cmd_metrics)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
