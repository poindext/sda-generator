"""
Patient Record router — single-patient SDA3 XML generation.
"""
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

router = APIRouter()


class CohortRef(BaseModel):
    template_file: str = ""
    cohort_id: str = ""


class RecordRequest(BaseModel):
    scenario: str
    filename: str = ""
    model: str = "gpt-4.1"
    cohort_refs: list[CohortRef] = []
    # legacy single-cohort fields kept for backwards compat
    template_file: str = ""
    cohort_id: str = ""


@router.get("/patient-record/download-zip")
async def download_zip(path: str):
    from app.config import BASE_DIR, POPULATIONS_DIR
    full = (BASE_DIR / path).resolve()
    pop_resolved = POPULATIONS_DIR.resolve()
    if not str(full).startswith(str(pop_resolved)):
        raise HTTPException(403)
    if not full.exists():
        raise HTTPException(404)
    return FileResponse(full, media_type="application/zip", filename=full.name)


@router.get("/patient-record/list")
async def list_patient_records():
    from datetime import datetime
    from app.config import BASE_DIR, POPULATIONS_DIR

    records_dir = POPULATIONS_DIR / "single-records"
    records_dir.mkdir(parents=True, exist_ok=True)

    results = []

    # Package directories (new format — one dir per generation)
    for pkg in sorted(records_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not pkg.is_dir():
            continue
        xml_files = sorted(pkg.glob("*.xml"))
        zip_files = list(pkg.glob("*.zip"))
        zip_path = str(zip_files[0].relative_to(BASE_DIR)) if zip_files else ""
        desc_file = pkg / "description.txt"
        scenario_file = pkg / "scenario.txt"
        description = ""
        if desc_file.exists():
            description = desc_file.read_text(encoding="utf-8").strip()[:140]
        elif scenario_file.exists():
            first_line = scenario_file.read_text(encoding="utf-8").split("\n")[0].strip()
            description = first_line[:140]
        results.append({
            "name": pkg.name,
            "type": "package",
            "file_count": len(xml_files),
            "files": [f.name for f in xml_files],
            "created": datetime.fromtimestamp(pkg.stat().st_mtime).isoformat(),
            "zip_path": zip_path,
            "description": description,
        })

    # Legacy single XML files (fallback / pre-split format)
    for f in sorted(records_dir.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True):
        results.append({
            "name": f.name,
            "type": "single",
            "file_count": 1,
            "files": [f.name],
            "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            "zip_path": "",
        })

    return {"records": results}


@router.get("/patient-record/cohort-templates")
async def list_cohort_templates():
    from app.services import patient_record_svc
    return {"options": patient_record_svc.list_cohort_options()}


@router.get("/patient-record/next-scenario")
async def get_next_scenario(package: str):
    """Return the scenario_next.txt for a package (used by the UI regenerate button)."""
    from app.config import BASE_DIR, POPULATIONS_DIR
    path = (POPULATIONS_DIR / "single-records" / package / "scenario_next.txt").resolve()
    records_resolved = (POPULATIONS_DIR / "single-records").resolve()
    if not str(path).startswith(str(records_resolved)):
        raise HTTPException(403)
    if not path.exists():
        raise HTTPException(404, "No refined scenario available for this package")
    return {"scenario": path.read_text(encoding="utf-8")}


@router.post("/patient-record/generate")
async def generate_patient_record(body: RecordRequest):
    from app.services import patient_record_svc

    async def _stream():
        # Normalise: merge legacy single-cohort fields into cohort_refs list
        refs = [{"template_file": r.template_file, "cohort_id": r.cohort_id}
                for r in body.cohort_refs]
        if not refs and body.template_file:
            refs = [{"template_file": body.template_file, "cohort_id": body.cohort_id}]
        async for event in patient_record_svc.generate_record(
            scenario=body.scenario,
            filename=body.filename or None,
            model=body.model,
            cohort_refs=refs,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
