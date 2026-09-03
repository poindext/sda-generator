"""
Generator service — wraps generate_population.py as a subprocess.

Supports multiple concurrent generation jobs.  Each job runs in its own
asyncio.to_thread() call and streams stdout to the job_store progress buffer.
After generation, optionally chains into QA, then builds zip chunks.
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import AsyncIterator

from app.config import BASE_DIR, SCRIPTS_DIR, POPULATIONS_DIR, GENERATION_WORKERS
from app.services import job_store
from app.services import qa_svc, chunker_svc


async def start_generation(
    job_id: str,
    template_path: str,
    output_dir: str,
    count: int,
    history_months: int,
    run_qa: bool,
    auto_fix: bool = True,
) -> None:
    """Fire-and-forget coroutine: run generation, then optionally QA + chunk."""
    asyncio.create_task(
        _generation_pipeline(job_id, template_path, output_dir,
                             count, history_months, run_qa, auto_fix)
    )


async def _generation_pipeline(
    job_id: str,
    template_path: str,
    output_dir: str,
    count: int,
    history_months: int,
    run_qa: bool,
    auto_fix: bool = True,
) -> None:
    job_store.update_status(job_id, "running")
    job_store.append_progress(job_id,
        f"Starting: {count} patients · template {Path(template_path).name}")

    try:
        success = await asyncio.to_thread(
            _run_generator_sync, job_id, template_path, output_dir, count, history_months
        )
    except Exception as exc:
        job_store.append_progress(job_id, f"[ERROR] {exc}")
        job_store.update_status(job_id, "failed", error=str(exc))
        return

    if not success:
        return

    if run_qa:
        job_store.append_progress(job_id, "")
        job_store.append_progress(job_id, "── Starting QA review ──────────────────")
        qa_success = await asyncio.to_thread(
            qa_svc.run_qa_sync, job_id, template_path, output_dir
        )
        pop_dir = Path(output_dir)
        meta = _read_meta(pop_dir)
        if qa_success:
            meta["qa_status"] = "approved"
            job_store.append_progress(job_id, "QA: APPROVED — building download chunks")
            await asyncio.to_thread(chunker_svc.build_chunks, pop_dir)
            meta["chunks_built"] = True
            _write_meta(pop_dir, meta)
            job_store.update_status(
                job_id, "completed",
                result={"output_dir": output_dir, "qa_status": "approved"},
            )
        elif auto_fix:
            meta["qa_status"] = "needs_review"
            _write_meta(pop_dir, meta)
            job_store.append_progress(
                job_id, "QA: ISSUES FOUND — starting auto-fix loop…"
            )
            from app.services import fixer_svc as _fixer  # lazy to avoid circular import
            await _fixer._fix_pipeline(
                job_id, template_path, output_dir, count, history_months
            )
        else:
            meta["qa_status"] = "needs_review"
            job_store.append_progress(job_id, "QA: ISSUES FOUND — building download chunks")
            await asyncio.to_thread(chunker_svc.build_chunks, pop_dir)
            meta["chunks_built"] = True
            _write_meta(pop_dir, meta)
            job_store.update_status(
                job_id, "completed",
                result={"output_dir": output_dir, "qa_status": "needs_review"},
            )
    else:
        pop_dir = Path(output_dir)
        meta = _read_meta(pop_dir)
        meta["qa_status"] = "skipped"
        job_store.append_progress(job_id, "Building download chunks...")
        await asyncio.to_thread(chunker_svc.build_chunks, pop_dir)
        meta["chunks_built"] = True
        _write_meta(pop_dir, meta)
        job_store.update_status(
            job_id, "completed",
            result={"output_dir": output_dir, "qa_status": "skipped"},
        )


def _run_generator_sync(
    job_id: str,
    template_path: str,
    output_dir: str,
    count: int,
    history_months: int,
) -> bool:
    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "generate_population.py"),
        "--template", template_path,
        "--output", output_dir,
        "--count", str(count),
        "--concurrency", str(GENERATION_WORKERS),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        cwd=str(BASE_DIR),
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    for line in proc.stdout:
        job_store.append_progress(job_id, line.rstrip())
    proc.wait()

    if proc.returncode != 0:
        job_store.update_status(job_id, "failed",
                                error=f"generator exited {proc.returncode}")
        return False
    return True


async def stream_job(job_id: str) -> AsyncIterator[str]:
    """Async generator yielding SSE data strings for any job backed by job_store."""
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


# --------------------------------------------------------------------------
# Population _meta.json helpers
# --------------------------------------------------------------------------

def _meta_path(pop_dir: Path) -> Path:
    return pop_dir / "_meta.json"


def _read_meta(pop_dir: Path) -> dict:
    import json
    p = _meta_path(pop_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _write_meta(pop_dir: Path, meta: dict) -> None:
    import json
    _meta_path(pop_dir).write_text(json.dumps(meta, indent=2), encoding="utf-8")
