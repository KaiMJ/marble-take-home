"""Persistent Python session for the agent. Runs as a child process.

Protocol: one JSON object per line on stdin ({"code": ...}), one JSON reply per line on stdout.
"""

import ast
import contextlib
import io
import json
import sqlite3
import sys
import traceback

import numpy as np
import pandas as pd

from checks import find_gaps
from db import ID_COLUMNS

pd.set_option("display.max_rows", 50)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 250)
pd.set_option("display.max_colwidth", 120)

DB_URI = f"file:{sys.argv[1]}?mode=ro"


def _con() -> sqlite3.Connection:
    return sqlite3.connect(DB_URI, uri=True)


def sql(query: str) -> pd.DataFrame:
    with _con() as con:
        return pd.read_sql_query(query, con)


def _load() -> dict[str, pd.DataFrame]:
    return {t: sql(f"SELECT * FROM {t}") for t in ID_COLUMNS}


def find(text: str) -> pd.DataFrame:
    """Every row in every table whose text contains `text` (case-insensitive)."""
    hits = []
    for table, id_col in ID_COLUMNS.items():
        df = ns[table]
        for col in df.columns:
            s = df[col].astype(str)
            mask = s.str.contains(text, case=False, regex=False)
            for idx in df.index[mask]:
                hits.append({"table": table, "id": df.at[idx, id_col], "column": col, "value": s[idx]})
    return pd.DataFrame(hits, columns=["table", "id", "column", "value"])


ns: dict = {"pd": pd, "np": np, "sql": sql, "find": find, **_load()}


def run(code: str, notes_until: str | None) -> dict:
    # Reload each call: chat notes and SKU mappings may have changed. During log replay, hide later notes.
    ns.update(_load())
    if notes_until:
        ns["notes"] = ns["notes"][ns["notes"].written_at <= notes_until].reset_index(drop=True)
    out = io.StringIO()
    result, error = None, None
    try:
        tree = ast.parse(code)
        last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        with contextlib.redirect_stdout(out):
            exec(compile(tree, "<agent>", "exec"), ns)
            if last is not None:
                value = eval(compile(ast.Expression(last.value), "<agent>", "eval"), ns)
                if value is not None:
                    result = repr(value)
    except Exception as e:
        # Show only the failing line of the agent's own code, not library internals.
        frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == "<agent>"]
        where = f"line {frames[-1].lineno}: {code.splitlines()[frames[-1].lineno - 1].strip()}\n" if frames else ""
        error = f"{where}{type(e).__name__}: {str(e).splitlines()[-1] if str(e) else ''}\n{_hint(e)}"
    return {"stdout": out.getvalue(), "result": result, "error": error, "empty": _empty_note(result),
            "gaps": _gap_note(code)}


GAP_TABLES = ("purchases", "catalog", "sales", "recipes", "raw_")


def _gap_note(code: str) -> str | None:
    """Warn when the code touches tables that have missing labels, so totals aren't trusted blindly."""
    if not any(t in code for t in GAP_TABLES):
        return None
    with _con() as con:
        gaps = find_gaps(con)
    if not gaps:
        return None
    lines = "\n".join(f"- {g['summary']}" for g in gaps)
    return f"Missing labels (see open.md); results using these tables may be incomplete:\n{lines}"


TABLES = ", ".join(ID_COLUMNS)


def _hint(e: Exception) -> str:
    """Point the model at the likely fix instead of leaving it to guess."""
    msg = str(e)
    if isinstance(e, KeyError):
        cols = {t: list(ns[t].columns) for t in ID_COLUMNS}
        owners = [t for t, c in cols.items() if msg.strip("'\"") in c]
        if owners:
            return f"Hint: column {msg} exists in: {', '.join(owners)}. Check which DataFrame you indexed."
        return f"Hint: {msg} is not a column of any preloaded table. Print df.columns to see what exists."
    if isinstance(e, NameError):
        return f"Hint: variables from earlier calls persist, but this name was never defined. Tables: {TABLES}."
    if "no such table" in msg:
        return f"Hint: available tables: {TABLES}, and raw_* copies of the CSVs."
    if "no such column" in msg:
        return "Hint: run sql('SELECT * FROM <table> LIMIT 1') to see the columns."
    return "Hint: fix the code and run it again; earlier variables are still defined."


def _empty_note(result: str | None) -> str | None:
    if result is not None and result.startswith("Empty DataFrame"):
        return ("The result has 0 rows. Check your filters before concluding nothing exists: "
                "comparisons like != '' or == 'x' also drop rows where the column is empty/NULL.")
    return None


for line in sys.stdin:
    req = json.loads(line)
    reply = run(req["code"], req.get("notes_until"))
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
