"""
SDA3 Population Generator — Web Application

Run from project root:
  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

IIS: HttpPlatformHandler starts uvicorn; see web.config.
"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from app.routers import wizard, templates, populations, jobs

app = FastAPI(title="SDA3 Population Generator", docs_url="/api/docs")

# --------------------------------------------------------------------------
# API routers
# --------------------------------------------------------------------------
app.include_router(wizard.router,      prefix="/api", tags=["wizard"])
app.include_router(templates.router,   prefix="/api", tags=["templates"])
app.include_router(populations.router, prefix="/api", tags=["populations"])
app.include_router(jobs.router,        prefix="/api", tags=["jobs"])

# --------------------------------------------------------------------------
# Static files and SPA fallback
# --------------------------------------------------------------------------
_static = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=_static), name="static")


@app.get("/")
async def index():
    return FileResponse(_static / "index.html")


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    # Let API routes bubble up naturally; only catch UI paths here
    if full_path.startswith("api/"):
        from fastapi import HTTPException
        raise HTTPException(404)
    return FileResponse(_static / "index.html")
