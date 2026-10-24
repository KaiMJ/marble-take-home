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
    # Reload each call: chat notes may have been added. During log replay, hide later notes.
    ns["notes"] = sql("SELECT * FROM notes")
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
    except Exception:
        error = "".join(traceback.format_exc().splitlines(keepends=True)[-5:])
    return {"stdout": out.getvalue(), "result": result, "error": error}


for line in sys.stdin:
    req = json.loads(line)
    reply = run(req["code"], req.get("notes_until"))
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
