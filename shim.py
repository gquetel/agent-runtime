#!/usr/bin/env python3
"""Runner for the autonomous vulnerability-research agent.

Driven by systemd as `vuln-agent@<mode>.service`, where <mode> is:

  nightly : started by a 23:00 timer. Runs until the next 04:00 (STOP_AT spans
            midnight from the 23:00 start). Refuses to run outside the
            23:00-04:00 window, so a stray start can't run all day.
  manual  : started by the guest poller when the operator drops a trigger. Runs
            for VA_MANUAL_MIN minutes (default 60), any time of day. A manual run
            preempts a nightly one (the poller stops nightly before starting it).

Each iteration runs one pass of the Operating Loop, then the shim decides whether
to start another. Stop conditions: the STOP_AT instant, or a usage signal from
the stream. The agent brain lives in CLAUDE.md; this script only orchestrates.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default

MODE = _env("VA_MODE", "nightly")  # "nightly" | "manual"

# Nightly window (local HH:MM). The session runs between START and END, with END
# on the following day — i.e. the window crosses midnight (23:00 -> 04:00).
NIGHT_START = _env("VA_NIGHT_START", "23:00")
NIGHT_END = _env("VA_CUTOFF", "04:00")
# Manual runs are wall-clock boxed to this many minutes instead.
MANUAL_MIN = _env_int("VA_MANUAL_MIN", 60)

# Backoff (seconds).
WAIT_SERVER_ERR = _env_int("VA_WAIT_SERVER_ERR", 120)
WAIT_CLEAN      = _env_int("VA_WAIT_CLEAN", 60)

CLAUDE_BIN = _env("VA_CLAUDE_BIN", "claude")
MODEL = _env("VA_MODEL", "sonnet")

# A manual run may carry a custom resume-prompt: the guest poller copies the
# operator's trigger text here (root-written, agent-readable) before starting us.
PROMPT_FILE = Path(_env("VA_PROMPT_FILE", "/work/state/manual.prompt"))

# Operator-facing status snapshot, read by the host-side `vuln-agent-run
# --status` CLI over the same shared state dir (virtiofs). Best-effort only —
# a failure to write it must never take down a session.
STATUS_FILE = Path(_env("VA_STATUS_FILE", "/work/state/status.json"))

DEFAULT_PROMPT = (
    "Read CLAUDE.md. Pull the active work item and its comments from Plane, "
    "run exactly one iteration of the Operating Loop, then stop."
)
RESUME_PROMPT = _env("VA_RESUME_PROMPT", DEFAULT_PROMPT)

# Absolute instant (epoch seconds) after which no new message may be sent and any
# in-flight iteration is killed. Set in main() once the mode is known.
STOP_AT = 0.0

# --------------------------------------------------------------------------- #
# Reactive detection patterns (checked against every streamed line)
# --------------------------------------------------------------------------- #

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

def matches_any(line: str, patterns) -> bool:
    return any(p.search(line) for p in patterns)

# --------------------------------------------------------------------------- #
# Clock window / stop instant
# --------------------------------------------------------------------------- #

def local_now() -> datetime:
    return datetime.now().astimezone()

def log(msg: str) -> None:
    """Print a shim-originated status line, prefixed with the current time.

    Reserved for the shim's own control-flow messages, not for lines relayed
    from the Claude invocation's stdout (raw passthrough or formatted stream
    events), which carry their own provenance and shouldn't look shim-timed.
    """
    print(f"[{local_now().strftime('%H:%M:%S')}] {msg}", flush=True)

def _hhmm(s: str) -> tuple[int, int]:
    hh, mm = (int(x) for x in s.split(":"))
    return hh, mm

def in_night_window(now: datetime | None = None) -> bool:
    """True if `now` is inside the [NIGHT_START, NIGHT_END) window that crosses
    midnight (e.g. 23:00 <= now, or now < 04:00)."""
    now = now or local_now()
    s_h, s_m = _hhmm(NIGHT_START)
    e_h, e_m = _hhmm(NIGHT_END)
    start = now.replace(hour=s_h, minute=s_m, second=0, microsecond=0)
    end = now.replace(hour=e_h, minute=e_m, second=0, microsecond=0)
    return now >= start or now < end

def next_night_end() -> datetime:
    """Next occurrence of NIGHT_END (today if still ahead, else tomorrow). From a
    23:00 start this lands on tomorrow 04:00, giving the full 5h window."""
    e_h, e_m = _hhmm(NIGHT_END)
    end = local_now().replace(hour=e_h, minute=e_m, second=0, microsecond=0)
    if local_now() >= end:
        end += timedelta(days=1)
    return end

def compute_stop_at() -> float:
    if MODE == "manual":
        return time.time() + MANUAL_MIN * 60
    return next_night_end().timestamp()

def load_manual_prompt() -> None:
    """Adopt the operator's custom resume-prompt for this manual run, if any.
    Read-only: the poller owns the file's lifecycle (agent can't write here)."""
    global RESUME_PROMPT
    try:
        txt = PROMPT_FILE.read_text().strip()
    except (FileNotFoundError, PermissionError):
        txt = ""
    if txt:
        RESUME_PROMPT = txt
        log("[request] manual run with custom prompt")
    else:
        log("[request] manual run with default prompt")

def preflight() -> tuple[bool, str]:
    """Return (ok_to_run, reason). ok_to_run=False means stop the session."""
    if MODE == "nightly" and not in_night_window():
        return False, "outside night window"
    if time.time() >= STOP_AT:
        return False, "stop instant reached"
    return True, "ok"

# --------------------------------------------------------------------------- #
# Operator status snapshot
# --------------------------------------------------------------------------- #

def write_status(state: str, **fields) -> None:
    """Atomically overwrite STATUS_FILE with the current status snapshot.

    Best-effort: the host CLI and a metrics scrape read this file, but its
    absence or a transient write failure must never affect the run itself.
    """
    payload = {
        "mode": MODE,
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

# --------------------------------------------------------------------------- #
# Docker cleanup between iterations (belt-and-suspenders; CLAUDE.md also prunes)
# --------------------------------------------------------------------------- #

def docker_cleanup() -> None:
    try:
        ids = subprocess.run(
            ["docker", "ps", "-q"], capture_output=True, text=True, timeout=30
        ).stdout.split()
        for cid in ids:
            subprocess.run(["docker", "stop", cid], capture_output=True, timeout=60)
            subprocess.run(["docker", "rm", cid], capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # docker is a fallback path; absence/hang is non-fatal

# --------------------------------------------------------------------------- #
# Stream formatting
# --------------------------------------------------------------------------- #

def _truncate(s: str, n: int = 120) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"

def extract_model(evt: dict) -> str | None:
    """Ground-truth model id as reported by Claude's own stream output, or None.

    Read from the stream (not our --model arg) so the log reflects what actually
    served the turn: the `system`/init event announces the resolved model, and
    each assistant message carries the model the API returned.
    """
    if evt.get("type") == "system" and evt.get("model"):
        return evt["model"]
    if evt.get("type") == "assistant":
        return evt.get("message", {}).get("model")
    return None

def format_stream_line(evt: dict) -> str | None:
    """Pretty one-liner for a stream-json event, or None to skip."""
    t = evt.get("type")
    if t == "assistant":
        parts = []
        for block in evt.get("message", {}).get("content", []):
            if block.get("type") == "text":
                parts.append(_truncate(block["text"]))
            elif block.get("type") == "tool_use":
                arg = ""
                inp = block.get("input", {})
                if isinstance(inp, dict) and inp:
                    first = next(iter(inp.values()))
                    arg = _truncate(first)
                parts.append(f"→ {block.get('name')}({arg})")
        return "  ".join(p for p in parts if p) or None
    if t == "user":
        for block in evt.get("message", {}).get("content", []):
            if block.get("type") == "tool_result":
                c = block.get("content")
                if isinstance(c, list):
                    c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                return f"  ⤶ {_truncate(c)}"
    if t == "result":
        return f"[result] {_truncate(evt.get('result', ''), 200)}"
    return None

# --------------------------------------------------------------------------- #
# One Claude invocation
# --------------------------------------------------------------------------- #

_child: subprocess.Popen | None = None

def run_claude() -> str:
    """Run one iteration. Returns an exit reason:
    'usage_limit' | 'server_error' | 'clean' | 'cutoff'.

    The iteration is killed at STOP_AT, so no message is sent past it.
    """
    global _child
    cmd = [
        CLAUDE_BIN, "--print",
        "--model", MODEL,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
        RESUME_PROMPT,
    ]
    log("=== iteration start ===")
    _child = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )

    reason = "clean"
    model_logged = False

    try:
        for line in _child.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            if not line:
                continue

            try:
                evt = json.loads(line)
            except json.JSONDecodeError:

                if matches_any(line, USAGE_LIMIT_PATTERNS):
                    reason = "usage_limit"
                    _kill_child()
                    break
                if matches_any(line, SERVER_ERROR_PATTERNS):
                    reason = "server_error"
                    log(f"[reactive] server_error line: {_truncate(line, 200)}")
                    _kill_child()
                    break
                print(line, flush=True)
                continue

            # Usage/server-limit signals also arrive as structured events (an
            # assistant text block, then a result event) — not just raw non-JSON
            # lines — so scan those too, else we sail past a session-limit stop
            # and loop straight back into the same wall. Server-error matching is
            # scoped to failed result events so a PoC merely discussing HTTP 5xx
            # doesn't trip it.
            et = evt.get("type")
            if et == "assistant":
                txt = " ".join(
                    b.get("text", "")
                    for b in evt.get("message", {}).get("content", [])
                    if b.get("type") == "text"
                )
                if matches_any(txt, USAGE_LIMIT_PATTERNS):
                    reason = "usage_limit"
                    _kill_child()
                    break
            elif et == "result":
                txt = str(evt.get("result", ""))
                if matches_any(txt, USAGE_LIMIT_PATTERNS):
                    reason = "usage_limit"
                    _kill_child()
                    break
                if evt.get("is_error") and matches_any(txt, SERVER_ERROR_PATTERNS):
                    reason = "server_error"
                    log(f"[reactive] server_error result: {_truncate(txt, 200)}")
                    _kill_child()
                    break

            if not model_logged:
                model = extract_model(evt)
                if model:
                    log(f"[model] serving turn with {model} (requested: {MODEL})")
                    model_logged = True

            pretty = format_stream_line(evt)
            if pretty:
                print(pretty, flush=True)

            if time.time() >= STOP_AT:
                reason = "cutoff"
                _kill_child()
                break
    finally:
        if _child:
            _child.wait()

    return reason

def _kill_child() -> None:
    global _child
    if _child and _child.poll() is None:
        _child.terminate()
        try:
            _child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _child.kill()

# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

_stop = False

def _on_term(signum, frame):
    global _stop
    _stop = True
    _kill_child()

def main() -> int:
    global STOP_AT
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    if MODE == "manual":
        load_manual_prompt()
    STOP_AT = compute_stop_at()
    started_at = local_now().isoformat()
    stop_at_iso = datetime.fromtimestamp(STOP_AT).astimezone().isoformat()
    log(f"[shim] mode={MODE} stop_at={stop_at_iso}")

    exit_reason = "stopped"  # overwritten on every path out of the loop below
    try:
        write_status("running", started_at=started_at, stop_at=stop_at_iso)

        while not _stop:
            ok, why = preflight()
            if not ok:
                log(f"[preflight] stopping: {why}")
                exit_reason = why
                break

            docker_cleanup()
            write_status(
                "running", started_at=started_at, stop_at=stop_at_iso,
                last_heartbeat=local_now().isoformat(),
            )
            reason = run_claude()

            if _stop:
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
                log(f"[reactive] server error — backing off {WAIT_SERVER_ERR}s")
                time.sleep(WAIT_SERVER_ERR)
            else:
                time.sleep(WAIT_CLEAN)
    except Exception as exc:
        exit_reason = f"crash: {exc!r}"
        log(f"[shim] unhandled exception: {exc!r}")
        raise
    finally:
        docker_cleanup()
        write_status("idle", last_exit_reason=exit_reason,
                     last_exit_at=local_now().isoformat())
        log("[shim] exiting")

    return 0

if __name__ == "__main__":
    sys.exit(main())
