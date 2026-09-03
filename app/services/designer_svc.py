"""
Designer service — wraps design_population.py as a subprocess and streams
its stdout back as SSE-compatible lines.
"""
import asyncio
import subprocess
import sys
from pathlib import Path
from typing import AsyncIterator

from app.config import BASE_DIR, SCRIPTS_DIR, DEFAULT_MODEL, OPENAI_API_KEY
from app.services import job_store


async def run_design(job_id: str, txt_path: str, template_path: str) -> None:
    """
    Run design_population.py in a background thread, streaming its output
    to the job's progress buffer.
    """
    job_store.update_status(job_id, "running")
    await asyncio.to_thread(
        _run_design_sync, job_id, txt_path, template_path
    )


def _run_design_sync(job_id: str, txt_path: str, template_path: str) -> None:
    env = _env_with_key()
    cmd = [
        sys.executable, "-u",
        str(SCRIPTS_DIR / "design_population.py"),
        txt_path,
        "--output", template_path,
        "--model", DEFAULT_MODEL,
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            cwd=str(BASE_DIR),
            env=env,
        )
    except Exception as exc:
        job_store.append_progress(job_id, f"[ERROR] {exc}")
        job_store.update_status(job_id, "failed", error=str(exc))
        return

    for line in proc.stdout:
        job_store.append_progress(job_id, line.rstrip())
    proc.wait()

    if proc.returncode == 0:
        job_store.update_status(job_id, "completed",
                                result={"template_path": template_path})
    else:
        job_store.update_status(job_id, "failed",
                                error=f"design_population.py exited {proc.returncode}")


async def stream_design(job_id: str) -> AsyncIterator[str]:
    """Async generator that yields SSE data strings for a design job."""
    sent = 0
    while True:
        lines = job_store.get_progress(job_id)
        while sent < len(lines):
            yield f"data: {lines[sent]}\n\n"
            sent += 1
        status = job_store.get_current_status(job_id)
        if status in ("completed", "failed", "cancelled"):
            yield f"data: [DONE:{status}]\n\n"
            break
        await asyncio.sleep(0.3)


def _env_with_key() -> dict:
    import os
    env = os.environ.copy()
    if OPENAI_API_KEY:
        env["OPENAI_API_KEY"] = OPENAI_API_KEY
    return env
