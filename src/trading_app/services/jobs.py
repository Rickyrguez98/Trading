"""Background jobs: run a pipeline CLI as a subprocess and track it on disk.

A full selection run takes a long time, so nothing waits on it: each job gets
a folder under ``data/app/jobs/<id>/`` holding ``job.json`` (state) and
``log.txt`` (the command's output), and the web layer polls. Commands are
argument lists, never shell strings, so form input cannot inject commands.
At most one job of each kind runs at a time.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

RUNNING, SUCCEEDED, FAILED = "running", "succeeded", "failed"


class JobConflict(RuntimeError):
    """A job of the same kind is already running."""


@dataclass
class Job:
    id: str
    kind: str                        # selection | allocation | monitor
    title: str
    argv: List[str]
    state: str = RUNNING
    created_at: str = ""
    finished_at: Optional[str] = None
    returncode: Optional[int] = None
    pid: Optional[int] = None
    outputs: Dict[str, str] = field(default_factory=dict)
    params: Dict[str, object] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.state in (SUCCEEDED, FAILED)

    @property
    def command(self) -> str:
        """For display: the module invocation without the interpreter path."""
        argv = list(self.argv)
        if len(argv) > 2 and argv[1] == "-m":
            argv = ["python", "-m"] + argv[2:]
        return " ".join(argv)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class JobManager:
    def __init__(self, jobs_dir: Path, *, cwd: Path, popen: Callable = subprocess.Popen) -> None:
        self.jobs_dir = Path(jobs_dir)
        self.cwd = Path(cwd)
        self._popen = popen
        self._procs: Dict[str, object] = {}

    # ------------------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def log_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "log.txt"

    def start(self, kind: str, title: str, argv: List[str], *,
              outputs: Optional[Dict[str, str]] = None, params: Optional[Dict[str, object]] = None,
              job_id: Optional[str] = None) -> Job:
        running = self.running(kind)
        if running is not None:
            raise JobConflict(f"a {kind} job is already running ({running.id}); wait for it to finish")
        job_id = job_id or self.new_id()
        d = self.job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, kind=kind, title=title, argv=[str(a) for a in argv], created_at=_now(),
                  outputs=dict(outputs or {}), params=dict(params or {}))
        log = open(self.log_path(job_id), "wb")
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        wrapped = [sys.executable, "-m", "trading_app.runner", str(d), "--", *job.argv]
        try:
            proc = self._popen(wrapped, cwd=str(self.cwd), stdout=log, stderr=subprocess.STDOUT, env=env)
        except Exception as exc:  # noqa: BLE001 - recorded on the job
            log.write(f"could not start: {type(exc).__name__}: {exc}\n".encode())
            log.close()
            job.state, job.returncode, job.finished_at = FAILED, -1, _now()
            self._save(job)
            return job
        log.close()
        job.pid = getattr(proc, "pid", None)
        self._procs[job_id] = proc
        self._save(job)
        return job

    @staticmethod
    def new_id() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

    # ------------------------------------------------------------------
    def get(self, job_id: str) -> Optional[Job]:
        path = self.job_dir(job_id) / "job.json"
        if not path.exists():
            return None
        job = Job(**json.loads(path.read_text(encoding="utf-8")))
        return self._refresh(job)

    def list(self, limit: int = 20) -> List[Job]:
        if not self.jobs_dir.exists():
            return []
        ids = sorted((p.name for p in self.jobs_dir.iterdir() if (p / "job.json").exists()), reverse=True)
        jobs = [self.get(i) for i in ids[:limit]]
        return [j for j in jobs if j is not None]

    def running(self, kind: str) -> Optional[Job]:
        return next((j for j in self.list(limit=50) if j.kind == kind and not j.done), None)

    def log_text(self, job_id: str, limit: int = 2_000_000) -> str:
        path = self.log_path(job_id)
        if not path.exists():
            return ""
        return path.read_bytes()[-limit:].decode("utf-8", errors="replace")

    def log_tail(self, job_id: str, lines: int = 40) -> str:
        data = self.log_text(job_id, 200_000)
        if not data:
            return ""
        # Progress bars rewrite one line with carriage returns: keep the latest frame.
        rows = [seg.rsplit("\r", 1)[-1] for seg in data.split("\n")]
        kept = [r for r in rows if r.strip()]
        return "\n".join(kept[-lines:])

    # ------------------------------------------------------------------
    def _refresh(self, job: Job) -> Job:
        if job.done:
            return job
        code_file = self.job_dir(job.id) / "exit_code"
        proc = self._procs.get(job.id)
        if code_file.exists():
            rc: Optional[int] = int(code_file.read_text(encoding="utf-8").strip() or -1)
            if proc is not None:
                _reap(proc)                # collect the finished wrapper so it is not left a zombie
        else:
            if proc is not None:
                rc = proc.poll()           # the wrapper ended without writing its file
                if rc is None:
                    return job
            elif job.pid is not None and _pid_alive(job.pid):
                return job                 # started by an earlier app process; still running
            else:
                rc = None                  # ended while the app was down, without a record
        job.returncode = rc
        job.state = SUCCEEDED if rc == 0 else FAILED
        job.finished_at = _now()
        self._procs.pop(job.id, None)
        self._save(job)
        return job

    def _save(self, job: Job) -> None:
        (self.job_dir(job.id) / "job.json").write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")


def _reap(proc) -> None:
    """Wait briefly for a wrapper that has written its exit code (it is about to exit)."""
    wait = getattr(proc, "wait", None)
    try:
        if wait is not None:
            wait(timeout=5)
        else:
            proc.poll()
    except Exception:  # noqa: BLE001 - reaping is housekeeping; the exit code is already recorded
        pass


# tqdm frames look like "Stage 2: prices:  45%|####5     | 2140/4757 [14:01<17:06,  2.54it/s]"
_BAR = re.compile(r"(?P<name>Stage \d+: [^:\r\n]+?):\s+(?P<pct>\d+)%\|[^|]*\|\s*(?P<n>\d+)/(?P<total>\d+)"
                  r"\s*\[(?P<elapsed>[\d:]+)(?:<(?P<eta>[\d:?]+))?")
SELECTION_STAGES = ("Stage 2: prices", "Stage 3: fundamentals", "Stage 4: news+sentiment")


def selection_progress(log: str) -> List[Dict[str, object]]:
    """The latest progress-bar frame of each selection stage, in order."""
    log = log or ""
    latest: Dict[str, Dict[str, object]] = {}
    last_frame = -1
    for m in _BAR.finditer(log):
        latest[m.group("name")] = {"name": m.group("name"), "pct": int(m.group("pct")), "n": int(m.group("n")),
                                   "total": int(m.group("total")), "elapsed": m.group("elapsed"),
                                   "eta": m.group("eta")}
        last_frame = m.end()
    # Paused right now: a rate-limit pause was logged and no progress has been made since.
    pausing = max(log.rfind("is rate limiting (429); pausing"), log.rfind("keeps answering 429")) > last_frame
    out = []
    for name in SELECTION_STAGES:
        if name in latest:
            row = dict(latest[name])
            row["done"] = row["n"] == row["total"]
            out.append(row)
    if out and not out[-1]["done"]:
        out[-1]["pausing"] = pausing
    return out


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
