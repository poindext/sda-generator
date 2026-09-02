"""
SDA3 Population Generator — Web Application

Run from project root:
  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

IIS sub-path (e.g. /genesis):
  Set GENESIS_ROOT_PATH=/genesis in environment; see web.config.
"""
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path

from app.routers import wizard, templates, populations, jobs

_root_path = os.environ.get("GENESIS_ROOT_PATH", "").rstrip("/")


class _StripRootPath:
    """ASGI middleware that removes a configured path prefix before routing.

    Needed for IIS sub-application deployments where HttpPlatformHandler
    forwards the full URL path (e.g. /genesis/api/...) to uvicorn.
    """
    def __init__(self, inner, root: str):
        self.inner = inner
        self.root = root

    async def __call__(self, scope, receive, send):
        if self.root and scope.get("type") in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self.root:
                # Redirect bare sub-path to trailing-slash form so that
                # relative asset URLs (static/css/app.css) resolve correctly.
                from starlette.responses import RedirectResponse
                await RedirectResponse(
                    url=self.root + "/", status_code=301
                )(scope, receive, send)
                return
            if path.startswith(self.root + "/"):
                scope = dict(scope)
                scope["path"] = path[len(self.root):] or "/"
        await self.inner(scope, receive, send)


_fastapi = FastAPI(title="SDA3 Population Generator", docs_url="/api/docs")

# --------------------------------------------------------------------------
# API routers
# --------------------------------------------------------------------------
_fastapi.include_router(wizard.router,      prefix="/api", tags=["wizard"])
_fastapi.include_router(templates.router,   prefix="/api", tags=["templates"])
_fastapi.include_router(populations.router, prefix="/api", tags=["populations"])
_fastapi.include_router(jobs.router,        prefix="/api", tags=["jobs"])

# --------------------------------------------------------------------------
# Static files and SPA fallback
# --------------------------------------------------------------------------
_static = Path(__file__).parent / "static"
_static_resolved = _static.resolve()


@_fastapi.get("/")
async def index():
    return FileResponse(_static / "index.html")


@_fastapi.get("/static/{file_path:path}")
async def serve_static(file_path: str):
    # Explicit route so Starlette's routing picks this up as a FULL match
    # before the catch-all, regardless of root_path in scope.
    candidate = (_static / file_path).resolve()
    if not str(candidate).startswith(str(_static_resolved)):
        raise HTTPException(403)
    if not candidate.exists():
        raise HTTPException(404)
    return FileResponse(candidate)


@_fastapi.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(_static / "index.html")


# Wrap with sub-path middleware when deployed under a non-root path
app = _StripRootPath(_fastapi, _root_path) if _root_path else _fastapi
