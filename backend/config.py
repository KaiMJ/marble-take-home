import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent
load_dotenv(ROOT / ".env")

LLM_MODEL = os.getenv("LLM_MODEL", "xai/grok-4.3-latest")
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low")

DATA_DIR = ROOT / "data"
OPS_LOG = ROOT / "docs" / "GM_OPS_LOG.md"

VAR_DIR = BACKEND / "data"
DB_PATH = VAR_DIR / "ops.db"
MEMORY_DIR = VAR_DIR / "memories"
RUNS_DIR = VAR_DIR / "runs"

# Snapshot time from the packet; the agent treats this as "now".
NOW = "Monday 2026-10-05 06:00 America/New_York"
PERIOD_START = date(2026, 9, 14)
PERIOD_END = date(2026, 10, 4)

MAX_STEPS = 25

# Print model reasoning and tool calls to stderr as the agent works (TRACE=0 to silence).
TRACE = os.getenv("TRACE", "1") != "0"
