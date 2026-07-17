#!/usr/bin/env python3
"""Convert a status.json snapshot (written by shim.py) into a node-exporter
textfile-collector .prom file.

Usage: status_to_prom.py <status.json path> <output .prom path>

Run by the host-side `vuln-agent-metrics` timer in nixconfigs, which mounts
the guest's shared state dir and has no other visibility into the agent's
run state. Kept alongside shim.py so the two stay in sync on schema changes.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path


def _escape(s: str) -> str:
    """Escape a string for use as a Prometheus label value."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <status.json> <output.prom>", file=sys.stderr)
        return 2

    status_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    if not status_path.exists():
        out_path.unlink(missing_ok=True)
        return 0

    status = json.loads(status_path.read_text())
    mode = _escape(str(status.get("mode", "unknown")))
    running = 1 if status.get("state") == "running" else 0
    reason = _escape(str(status.get("last_exit_reason") or "none")[:200])
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


if __name__ == "__main__":
    sys.exit(main())
