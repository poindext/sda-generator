"""
Populations router — list, detail, download, delete.
Download is gated: if QA was requested, the population must be approved first.
"""
import csv
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import POPULATIONS_DIR
from app.services.chunker_svc import list_chunks

router = APIRouter()


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------

@router.get("/populations")
async def list_populations():
    pops = []
    for d in sorted(POPULATIONS_DIR.iterdir(),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir() or d.name.startswith("."):
            continue
        meta = _read_meta(d)
        if not meta:
            continue   # skip dirs that aren't managed populations
        pops.append(_pop_summary(d, meta))
    return {"populations": pops}


# --------------------------------------------------------------------------
# Detail
# --------------------------------------------------------------------------

@router.get("/populations/{pop_id}")
async def get_population(pop_id: str):
    d = _find(pop_id)
    meta = _read_meta(d)
    detail = _pop_summary(d, meta)

    # Patient count from CSV
    patients_csv = d / "patients.csv"
    if patients_csv.exists():
        with open(patients_csv, newline="", encoding="utf-8") as f:
            detail["patient_count"] = sum(1 for _ in csv.reader(f)) - 1
    else:
        detail["patient_count"] = 0

    # QA issues
    qa_file = d / "qa_issues.json"
    if qa_file.exists():
        qa_data = json.loads(qa_file.read_text(encoding="utf-8"))
        detail["qa_issues"] = qa_data.get("issues", [])
        detail["qa_summary"] = qa_data.get("summary", {})
    else:
        detail["qa_issues"] = []
        detail["qa_summary"] = {}

    # Validation warnings
    val_csv = d / "generator_validation.csv"
    if val_csv.exists():
        with open(val_csv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        detail["validation_warnings"] = len(rows)
    else:
        detail["validation_warnings"] = 0

    # Chunks
    detail["chunks"] = list_chunks(d)

    return detail


# --------------------------------------------------------------------------
# Download chunk
# --------------------------------------------------------------------------

@router.get("/populations/{pop_id}/chunks/{chunk_name}")
async def download_chunk(pop_id: str, chunk_name: str):
    d = _find(pop_id)
    meta = _read_meta(d)

    if not _is_downloadable(meta):
        raise HTTPException(
            403,
            "Population is not yet downloadable — QA review is required and has not been approved.",
        )

    chunk_path = d / "chunks" / chunk_name
    if not chunk_path.exists() or chunk_path.suffix != ".zip":
        raise HTTPException(404, "Chunk not found")

    return FileResponse(
        path=str(chunk_path),
        filename=chunk_name,
        media_type="application/zip",
    )


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------

@router.delete("/populations/{pop_id}")
async def delete_population(pop_id: str):
    d = _find(pop_id)
    shutil.rmtree(d)
    return {"deleted": True, "id": pop_id}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _find(pop_id: str) -> Path:
    d = POPULATIONS_DIR / pop_id
    if not d.is_dir():
        raise HTTPException(404, f"Population not found: {pop_id}")
    return d


def _read_meta(pop_dir: Path) -> dict | None:
    p = pop_dir / "_meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pop_summary(d: Path, meta: dict) -> dict:
    return {
        "id": d.name,
        "population_name": meta.get("population_name", d.name),
        "template_id": meta.get("template_id", ""),
        "count_requested": meta.get("count", 0),
        "run_qa": meta.get("run_qa", False),
        "qa_status": meta.get("qa_status", "unknown"),
        "chunks_built": meta.get("chunks_built", False),
        "downloadable": _is_downloadable(meta),
        "job_id": meta.get("job_id", ""),
        "modified": d.stat().st_mtime,
    }


def _is_downloadable(meta: dict) -> bool:
    if not meta.get("chunks_built", False):
        return False
    qa_status = meta.get("qa_status", "skipped")
    if meta.get("run_qa", False):
        return qa_status == "approved"
    return True
