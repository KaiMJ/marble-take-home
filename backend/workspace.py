"""`python` tool: client for the persistent session in workspace_server.py, plus the manifest."""

import json
import select
import sqlite3
import subprocess
import sys

import pandas as pd

import config
from db import ID_COLUMNS

TIMEOUT_S = 30
MAX_OUTPUT = 8000

SCHEMA = {
    "type": "function",
    "function": {
        "name": "python",
        "description": (
            "Run Python in a persistent analysis session. Variables persist across calls. "
            "Tables are preloaded as pandas DataFrames (see WORKSPACE); sql(query) and find(text) "
            "are available. Read-only: you cannot modify the data. Print what you want to see; "
            "the value of a final bare expression is also shown."
        ),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
}

TABLE_DESCRIPTIONS = {
    "purchases": "one row per invoice line; base_qty in base units (lb or each) via the catalog pack size; "
                 "ingredient is empty where the SKU's description doesn't match a kitchen ingredient name",
    "invoices": "one row per invoice; entered_status is typed by the office, not scanned at receiving",
    "sales": "one row per POS item per day; days without sales of an item omit that item",
    "recipes": "ingredient quantity per portion, by menu item, with effective date",
    "counts": "physical inventory counts (opening and closing only)",
    "menu": "POS buttons, listed prices, planned availability",
    "vendors": "vendor order sheet as supplied (dated; may be stale)",
    "catalog": "supplier SKUs, pack quantity and unit; ingredient set only on an exact name match, else empty",
    "notes": "shift notebook (source='log', N##) and chat messages (source='chat', U##)",
}


class Workspace:
    def __init__(self, notes_until: str | None = None) -> None:
        self.proc: subprocess.Popen | None = None
        self.notes_until = notes_until

    def _start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(config.BACKEND / "workspace_server.py"), str(config.DB_PATH)],
            cwd=config.BACKEND,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def run(self, code: str) -> str:
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        self.proc.stdin.write(json.dumps({"code": code, "notes_until": self.notes_until}) + "\n")
        self.proc.stdin.flush()
        ready, _, _ = select.select([self.proc.stdout], [], [], TIMEOUT_S)
        if not ready:
            self.close()
            return f"<error>Timed out after {TIMEOUT_S}s. Session restarted; variables were lost.</error>"
        reply = json.loads(self.proc.stdout.readline())
        parts = []
        if reply["stdout"]:
            parts.append(f"<stdout>{reply['stdout']}</stdout>")
        if reply["result"] is not None:
            parts.append(f"<result>{reply['result']}</result>")
        if reply.get("gaps"):
            parts.append(f"<note>{reply['gaps']}</note>")
        if reply.get("empty"):
            parts.append(f"<note>{reply['empty']}</note>")
        if reply["error"]:
            parts.append(f"<error>{reply['error']}</error>")
        text = "\n".join(parts) or "<stdout></stdout>"
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + f"\n[truncated: {len(text) - MAX_OUTPUT} more chars]"
        return text

    def close(self) -> None:
        if self.proc:
            self.proc.kill()
            self.proc = None


def manifest() -> str:
    lines = ["WORKSPACE (python tool): these DataFrames are preloaded."]
    with sqlite3.connect(config.DB_PATH) as con:
        for table in ID_COLUMNS:
            df = pd.read_sql_query(f"SELECT * FROM {table}", con)
            cols = ", ".join(f"{c} {_dtype(df[c])}" for c in df.columns)
            lines.append(f"\n{table} ({len(df)} rows): {TABLE_DESCRIPTIONS[table]}")
            lines.append(f"  columns: {cols}")
            for _, row in df.head(2).iterrows():
                lines.append("  e.g. " + " | ".join(_cell(v) for v in row))
        ingredients = [r[0] for r in con.execute("SELECT DISTINCT ingredient FROM counts")]
    lines.append("\nhelpers: sql(query) -> DataFrame over these tables and raw_* copies of the CSVs; "
                 "find(text) -> DataFrame of every row mentioning an ID or term")
    lines.append("ingredients: " + "; ".join(ingredients))
    return "\n".join(lines)


def _dtype(s: pd.Series) -> str:
    return {"i": "int", "f": "float"}.get(s.dtype.kind, "str")


def _cell(v) -> str:
    v = str(v)
    return v if len(v) <= 40 else v[:37] + "..."
