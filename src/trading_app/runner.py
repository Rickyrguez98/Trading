"""Run one job command and record its exit code next to its log.

    python -m trading_app.runner <job_dir> -- <command...>

The job manager starts commands through this wrapper so a job's outcome is
known even if the app was restarted while the job ran: the exit code is
written to ``<job_dir>/exit_code`` when the command ends.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--":
        print("usage: python -m trading_app.runner <job_dir> -- <command...>", file=sys.stderr)
        return 2
    job_dir, command = Path(args[0]), args[2:]
    try:
        rc = subprocess.call(command)
    except OSError as exc:
        print(f"could not run {command[0]}: {exc}", file=sys.stderr)
        rc = 127
    tmp = job_dir / "exit_code.tmp"
    tmp.write_text(str(rc), encoding="utf-8")
    tmp.replace(job_dir / "exit_code")
    return rc


if __name__ == "__main__":
    sys.exit(main())
