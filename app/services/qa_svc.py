"""
QA service — wraps auto_qa.py as a subprocess.
Returns True if the population is approved (exit 0), False if issues found (exit 1).
"""
import subprocess
import sys
from pathlib import Path

from app.config import BASE_DIR, SCRIPTS_DIR, OPENAI_API_KEY, DEFAULT_MODEL
from app.services import job_store


def run_qa_sync(job_id: str, template_path: str, output_dir: str) -> bool:
    """
    Run auto_qa.py synchronously (called via asyncio.to_thread).
    Streams stdout to the job progress buffer.
    Returns True if approved, False if issues found or error.
    """
    env = _env_with_key()
    cmd = [
        sys.executable, "-u",
        str(SCRIPTS_DIR / "auto_qa.py"),
        "--template", template_path,
        "--output", output_dir,
        "--model", DEFAULT_MODEL,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        cwd=str(BASE_DIR),
        env=env,
    )
    for line in proc.stdout:
        job_store.append_progress(job_id, line.rstrip())
    proc.wait()

    # exit 0 = approved, exit 1 = issues found, exit 2 = runtime error
    return proc.returncode == 0


def _env_with_key() -> dict:
    import os
    env = os.environ.copy()
    if OPENAI_API_KEY:
        env["OPENAI_API_KEY"] = OPENAI_API_KEY
    return env
