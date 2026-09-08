"""
Patient Record router — single-patient SDA3 XML generation.
"""
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

router = APIRouter()


class RecordRequest(BaseModel):
    scenario: str
    filename: str = ""
    model: str = "gpt-4o"
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


@router.get("/patient-record/cohort-templates")
async def list_cohort_templates():
    from app.services import patient_record_svc
    return {"options": patient_record_svc.list_cohort_options()}


@router.post("/patient-record/generate")
async def generate_patient_record(body: RecordRequest):
    from app.services import patient_record_svc

    async def _stream():
        async for event in patient_record_svc.generate_record(
            scenario=body.scenario,
            filename=body.filename or None,
            model=body.model,
            template_file=body.template_file,
            cohort_id=body.cohort_id,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
