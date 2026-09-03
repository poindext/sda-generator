"""
Central configuration — all paths and settings derived from the project root.
Loads OPENAI_API_KEY from .env if present, then falls back to environment.
"""
from pathlib import Path
import os

BASE_DIR = Path(__file__).parent.parent          # project root
SCRIPTS_DIR  = BASE_DIR / "scripts"
TEMPLATES_DIR = BASE_DIR / "templates"
POPULATIONS_DIR = BASE_DIR / "populations"
CONFIG_DIR   = BASE_DIR / "config"
JOBS_DIR     = BASE_DIR / "jobs"

for _d in (TEMPLATES_DIR, POPULATIONS_DIR, JOBS_DIR):
    _d.mkdir(exist_ok=True)

# Load .env if present (never overrides real environment variables)
_env = BASE_DIR / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
DEFAULT_MODEL: str  = "gpt-4o"
CHUNK_SIZE: int     = 10_000
# Workers for generate_population.py subprocess.
# Windows (IIS) spawns a full Python interpreter per worker — keep at 1
# to avoid multiple ~300 MB Python processes exhausting the app pool.
GENERATION_WORKERS: int = int(os.environ.get("GENERATION_WORKERS", "1"))
