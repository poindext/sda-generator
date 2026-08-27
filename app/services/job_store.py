"""
In-memory + on-disk job store.

Jobs are stored as JSON files in jobs/.  In-memory dicts hold live progress
lines and status so the SSE streaming endpoint never has to hit the disk on
every poll tick.

Thread safety: list.append() is GIL-protected; status updates use a lock.
"""
import json
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import JOBS_DIR

_lock = threading.Lock()
_progress: dict[str, list[str]] = {}   # job_id -> accumulated stdout lines
_status:   dict[str, str]       = {}   # job_id -> current status


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    type: str           # "generate" | "qa" | "design"
    status: str         # "pending" | "running" | "completed" | "failed" | "cancelled"
    created_at: str
    params: dict        = field(default_factory=dict)
    started_at: Optional[str]   = None
    completed_at: Optional[str] = None
    error: Optional[str]        = None
    result: Optional[dict]      = None


# --------------------------------------------------------------------------
# Persistence helpers
# --------------------------------------------------------------------------

def _path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _save(job: Job) -> None:
    _path(job.id).write_text(
        json.dumps(asdict(job), indent=2), encoding="utf-8"
    )


def _load_file(job_id: str) -> Optional[Job]:
    p = _path(job_id)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return Job(**data)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def create_job(job_type: str, params: dict) -> Job:
    job = Job(
        id=f"job_{uuid.uuid4().hex[:12]}",
        type=job_type,
        status="pending",
        created_at=_now(),
        params=params,
    )
    with _lock:
        _progress[job.id] = []
        _status[job.id]   = "pending"
    _save(job)
    return job


def get_job(job_id: str) -> Optional[Job]:
    job = _load_file(job_id)
    if job and job_id in _status:
        job.status = _status[job_id]
    return job


def list_jobs() -> list[Job]:
    jobs = []
    for p in sorted(JOBS_DIR.glob("job_*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        data = json.loads(p.read_text(encoding="utf-8"))
        job = Job(**data)
        jid = job.id
        if jid in _status:
            job.status = _status[jid]
        jobs.append(job)
    return jobs


def update_status(job_id: str, status: str,
                  error: str = None, result: dict = None) -> None:
    with _lock:
        _status[job_id] = status
    job = _load_file(job_id)
    if not job:
        return
    job.status = status
    if status == "running" and not job.started_at:
        job.started_at = _now()
    if status in ("completed", "failed", "cancelled"):
        job.completed_at = _now()
    if error:
        job.error = error
    if result:
        job.result = result
    _save(job)


def append_progress(job_id: str, line: str) -> None:
    if job_id in _progress:
        _progress[job_id].append(line)


def get_progress(job_id: str) -> list[str]:
    return list(_progress.get(job_id, []))


def get_current_status(job_id: str) -> str:
    return _status.get(job_id, "unknown")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
