import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR.parent / "data"
DOCS_DIR = BACKEND_DIR.parent / "docs"  # GM_OPS_LOG.md, SITE_PROFILE.md
VAR_DIR = BACKEND_DIR / "var"

load_dotenv(BACKEND_DIR / ".env")

LLM_MODEL = os.getenv("LLM_MODEL", "xai/grok-4.3-latest")
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low")
DB_PATH = VAR_DIR / "memory.db"
CONTEXT_PATH = VAR_DIR / "context.json"

# "Now" for this packet: Monday, Oct 5, 2026, 6 a.m.
NOW = "2026-10-05T06:00:00"
