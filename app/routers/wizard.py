"""
Wizard router — LLM interview sessions and cohort catalog.
"""
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import CONFIG_DIR
from app.services import wizard_svc

router = APIRouter()


# --------------------------------------------------------------------------
# Cohort catalog
# --------------------------------------------------------------------------

@router.get("/cohorts")
async def list_catalog_cohorts():
    """Return approved cohorts from cohort_catalog.json."""
    catalog_path = CONFIG_DIR / "cohort_catalog.json"
    if not catalog_path.exists():
        return {"cohorts": []}
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    cohorts = []
    for slug, entry in data.get("cohorts", {}).items():
        cohorts.append({
            "id": slug,
            "name": entry.get("canonical_name", slug),
            "qa_status": entry.get("qa_status", "approved"),
            "keywords": entry.get("keywords", []),
        })
    return {"cohorts": cohorts}


# --------------------------------------------------------------------------
# Wizard sessions
# --------------------------------------------------------------------------

class StartSession(BaseModel):
    selected_cohorts: list[str] = []


@router.post("/wizard/sessions")
async def create_session(body: StartSession):
    session_id = wizard_svc.create_session(body.selected_cohorts)
    return {"session_id": session_id}


@router.get("/wizard/sessions/{session_id}/opening")
async def stream_opening(session_id: str):
    """Stream the wizard's opening question."""
    async def _gen():
        try:
            async for chunk in wizard_svc.opening_message(session_id):
                payload = json.dumps({"type": "token", "content": chunk})
                yield f"data: {payload}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except KeyError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Session not found'})}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class UserMessage(BaseModel):
    content: str


@router.post("/wizard/sessions/{session_id}/message")
async def send_message(session_id: str, body: UserMessage):
    """Stream the assistant reply to a user message."""
    async def _gen():
        try:
            async for event in wizard_svc.send_message(session_id, body.content):
                yield f"data: {json.dumps(event)}\n\n"
        except KeyError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Session not found'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/wizard/sessions/{session_id}/synthesize")
async def synthesize(session_id: str):
    """Stream the final population description document synthesis."""
    async def _gen():
        try:
            async for event in wizard_svc.synthesize_document(session_id):
                yield f"data: {json.dumps(event)}\n\n"
        except KeyError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Session not found'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/wizard/sessions/{session_id}/document")
async def get_document(session_id: str):
    doc = wizard_svc.get_document(session_id)
    if doc is None:
        raise HTTPException(404, "Document not yet generated")
    return {"document": doc}
