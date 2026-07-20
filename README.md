# vuln-agent-runtime

The runtime "brain" of the nightly autonomous vulnerability-research agent:
the operating loop (`CLAUDE.md`), the CLI that drives Claude Code through it
and reports on it (`vuln_agent.py`), and the pre-accepted onboarding state
(`claude.json`).

This repo has no deployment logic of its own. It's pinned via `npins` from
the `vuln-agent` module in `nixconfigs` (`modules/vuln-agent/guest.nix` and
`default.nix`), which is where the microVM, network fencing, secrets, and
systemd units that actually run this code live. Changes here only take
effect after that pin is updated (`npins update vuln-agent-runtime` in
`nixconfigs`) and the host is redeployed.

## Files

- `CLAUDE.md` — the agent's operating loop: what it should do each iteration,
  where it looks for work, how it reports back.
- `claude.json` — placed at `~/.claude.json` before each start so the
  headless run doesn't hang on a first-run trust prompt.
- `vuln_agent.py` — the orchestration CLI, two subcommands:
  - `vuln-agent run --mode {nightly,manual} [options]` — drives one
    `claude --print` invocation per iteration inside a systemd-managed
    session: night-window/wall-clock cutoffs, usage-limit and server-error
    backoff, and an operator-facing `status.json` snapshot written after
    every iteration and in a `finally` block on crash. Run `--help` for the
    full option list (all of it also has sane defaults).
  - `vuln-agent metrics <status.json> <output.prom>` — converts a
    `status.json` snapshot into a node-exporter textfile-collector metric.
    Kept in the same module as `run` (not a separate script) so the metric
    and the schema it reads can never drift apart.

## status.json

Written by `run` to `/work/state/status.json` (shared with the host over
virtiofs) on every state change:

```json
{
  "mode": "manual",
  "state": "running",
  "updated_at": "2026-07-17T23:00:04+02:00",
  "started_at": "2026-07-17T23:00:01+02:00",
  "stop_at": "2026-07-18T04:00:00+02:00",
  "last_heartbeat": "2026-07-17T23:12:30+02:00"
}
```

`state` is `"running"` from start until the process is about to exit, at
which point it flips to `"idle"` and `last_exit_reason`/`last_exit_at` are
set — including on an unhandled exception, via a `finally` block. The
host-side `vuln-agent-run --status` CLI and `vuln-agent metrics` both read
this file directly; neither needs to reach into the guest.
