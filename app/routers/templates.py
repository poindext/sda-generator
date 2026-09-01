"""
Template router — CRUD for .template.json files in templates/.
"""
import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import TEMPLATES_DIR

router = APIRouter()


# --------------------------------------------------------------------------
# List / get
# --------------------------------------------------------------------------

@router.get("/templates")
async def list_templates():
    templates = []
    for p in sorted(TEMPLATES_DIR.glob("*.template.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            meta = data.get("meta", {})
        except Exception:
            meta = {}
        templates.append({
            "id": p.stem.replace(".template", ""),
            "filename": p.name,
            "name": meta.get("name", p.stem),
            "description": meta.get("description", ""),
            "state": meta.get("state", ""),
            "total_patients": meta.get("total_patients"),
            "cohort_count": len(data.get("cohorts", [])) if "data" in dir() else 0,
            "modified": p.stat().st_mtime,
        })
    return {"templates": templates}


@router.get("/templates/{template_id}")
async def get_template(template_id: str):
    p = _find(template_id)
    return {"id": template_id, "content": json.loads(p.read_text(encoding="utf-8"))}


# --------------------------------------------------------------------------
# Save / update
# --------------------------------------------------------------------------

class TemplateSave(BaseModel):
    content: dict


@router.put("/templates/{template_id}")
async def save_template(template_id: str, body: TemplateSave):
    p = _find(template_id)
    p.write_text(json.dumps(body.content, indent=2), encoding="utf-8")
    return {"saved": True, "id": template_id}


# --------------------------------------------------------------------------
# Save description document as a .txt + associate with a template
# --------------------------------------------------------------------------

class SaveDescription(BaseModel):
    name: str          # base name for both .txt and .template.json
    document: str      # the .txt content from the wizard synthesis


@router.post("/templates/description")
async def save_description(body: SaveDescription):
    slug = _slugify(body.name)
    txt_path = TEMPLATES_DIR / f"{slug}.txt"
    txt_path.write_text(body.document, encoding="utf-8")
    return {"txt_path": str(txt_path), "template_id": slug}


# --------------------------------------------------------------------------
# Design (chain into design_population.py)
# --------------------------------------------------------------------------

class DesignRequest(BaseModel):
    template_id: str   # slug; .txt file must already exist


@router.post("/templates/{template_id}/design")
async def start_design(template_id: str):
    from app.services import job_store, designer_svc
    import asyncio

    txt_path = TEMPLATES_DIR / f"{template_id}.txt"
    if not txt_path.exists():
        raise HTTPException(404, f"Description file not found: {txt_path.name}")

    template_path = str(TEMPLATES_DIR / f"{template_id}.template.json")
    job = job_store.create_job("design", {
        "template_id": template_id,
        "txt_path": str(txt_path),
        "template_path": template_path,
    })
    asyncio.create_task(
        designer_svc.run_design(job.id, str(txt_path), template_path)
    )
    return {"job_id": job.id}


# --------------------------------------------------------------------------
# Import / export / delete
# --------------------------------------------------------------------------

@router.post("/templates/import")
async def import_template(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")
    name = Path(file.filename).stem.replace(".template", "")
    slug = _slugify(name) or f"template_{uuid.uuid4().hex[:8]}"
    dest = TEMPLATES_DIR / f"{slug}.template.json"
    dest.write_bytes(raw)
    return {"id": slug, "filename": dest.name}


@router.get("/templates/{template_id}/export")
async def export_template(template_id: str):
    p = _find(template_id)
    return FileResponse(
        path=str(p),
        filename=p.name,
        media_type="application/json",
    )


@router.delete("/templates/{template_id}")
async def delete_template(template_id: str):
    p = _find(template_id)
    txt = TEMPLATES_DIR / f"{template_id}.txt"
    p.unlink()
    if txt.exists():
        txt.unlink()
    return {"deleted": True}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _find(template_id: str) -> Path:
    p = TEMPLATES_DIR / f"{template_id}.template.json"
    if not p.exists():
        raise HTTPException(404, f"Template not found: {template_id}")
    return p


def _slugify(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9_-]", "_", name.lower().strip())[:64]
