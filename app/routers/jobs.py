"""
Jobs router — generation job creation, status, streaming progress.
"""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import POPULATIONS_DIR, TEMPLATES_DIR
from app.services import job_store, generator_svc

router = APIRouter()


class GenerateRequest(BaseModel):
    template_id: str
    population_name: str
    count: int = 100
    history_months: int = 24
    run_qa: bool = False
    auto_fix: bool = True


@router.post("/jobs")
async def create_job(body: GenerateRequest):
    template_path = TEMPLATES_DIR / f"{body.template_id}.template.json"
    if not template_path.exists():
        raise HTTPException(404, f"Template not found: {body.template_id}")

    slug = _slugify(body.population_name)
    output_dir = str(POPULATIONS_DIR / slug)

    job = job_store.create_job("generate", {
        "template_id": body.template_id,
        "template_path": str(template_path),
        "output_dir": output_dir,
        "population_name": body.population_name,
        "count": body.count,
        "history_months": body.history_months,
        "run_qa": body.run_qa,
        "auto_fix": body.auto_fix,
    })

    # Write _meta.json immediately so the population appears in listings
    pop_dir = Path(output_dir)
    pop_dir.mkdir(parents=True, exist_ok=True)
    _write_meta(pop_dir, {
        "job_id": job.id,
        "template_id": body.template_id,
        "population_name": body.population_name,
        "count": body.count,
        "run_qa": body.run_qa,
        "auto_fix": body.auto_fix,
        "qa_status": "pending" if body.run_qa else "skipped",
        "chunks_built": False,
    })

    await generator_svc.start_generation(
        job_id=job.id,
        template_path=str(template_path),
        output_dir=output_dir,
        count=body.count,
        history_months=body.history_months,
        run_qa=body.run_qa,
        auto_fix=body.auto_fix,
    )

    return {"job_id": job.id, "output_dir": output_dir}


@router.get("/jobs")
async def list_jobs():
    jobs = job_store.list_jobs()
    return {"jobs": [_job_dict(j) for j in jobs]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_dict(job)


@router.get("/jobs/{job_id}/lines")
async def get_job_lines(job_id: str, since: int = 0):
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    lines = job_store.get_progress(job_id)
    return {"lines": lines[since:], "status": job.status}


@router.get("/jobs/{job_id}/stream")
async def stream_job_progress(job_id: str):
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    return StreamingResponse(
        generator_svc.stream_job(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status not in ("pending", "running"):
        raise HTTPException(400, f"Cannot cancel job in status: {job.status}")
    job_store.update_status(job_id, "cancelled")
    return {"cancelled": True}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _job_dict(job) -> dict:
    from dataclasses import asdict
    return asdict(job)


def _write_meta(pop_dir: Path, meta: dict) -> None:
    (pop_dir / "_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def _slugify(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9_-]", "_", name.lower().strip())[:64]
