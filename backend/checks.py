"""Missing-label checks: records the data can't be interpreted without.

The checks only detect gaps; they never fill them. Gaps become questions under Open in memory.md.
Answers and corrections are recorded under its Data section ('- map ...', '- set ...') and applied to
the normalized tables (raw_* stay untouched), so every later query sees them with an overridden_by source.
"""

import re
import sqlite3

import pandas as pd

import config
import db

MAP_LINE = re.compile(r"^- map (\S+) -> (.+?)\s*\[(.+?)\]", re.M)
TAG = "(data check: {key})"


def find_gaps(con: sqlite3.Connection) -> list[dict]:
    gaps = []
    for sku, desc, vendor, cat_id, lines in con.execute("""
        SELECT c.sku, c.description, c.vendor_id, c.catalog_row_id, GROUP_CONCAT(p.line_id, ', ')
        FROM catalog c LEFT JOIN purchases p ON p.sku = c.sku
        WHERE c.ingredient IS NULL GROUP BY c.sku"""):
        gaps.append({
            "key": f"sku:{sku}",
            "summary": f"{lines or 'no'} purchase lines have no ingredient (SKU {sku})",
            "question": (f"SKU {sku} ({desc!r}, vendor {vendor}) isn't linked to a kitchen ingredient, so its "
                         f"purchase lines are missing from ingredient totals [{cat_id}, {lines or 'no lines'}]. "
                         f"Which ingredient is it? Record as '- map {sku} -> <ingredient> [sources]' under Data in memory.md."),
        })
    for item_id, name, rows in con.execute("""
        SELECT s.item_id, s.item, GROUP_CONCAT(s.row_id, ', ')
        FROM sales s WHERE s.item_id NOT IN (SELECT item_id FROM recipes) GROUP BY s.item_id"""):
        gaps.append({
            "key": f"recipe:{item_id}",
            "summary": f"{item_id} {name} sells but has no recipe, so its ingredient use is not counted",
            "question": (f"{item_id} {name} has sales but no recipe card [{item_id}, {rows.split(', ')[0]} ...]. "
                         "What goes into one portion?"),
        })
    return gaps


def kitchen_ingredients(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT ingredient FROM counts UNION SELECT ingredient FROM recipes")}


SET_LINE = re.compile(r"^- set (vendor|invoice|line) (\S+) (\w+) = (.+?)(?: from (\d{4}-\d{2}-\d{2}))?\s*\[(.+?)\]",
                      re.MULTILINE)
# What memory may override, and where: kind -> (table, key column, allowed fields).
OVERRIDES = {
    "vendor": ("vendors", "vendor_id", {"delivery_days", "order_cutoff"}),
    "invoice": ("invoices", "invoice_id", {"entered_status"}),
    "line": ("purchases", "line_id", {"unit_price", "quantity"}),
}


NUMERIC_FIELDS = {"unit_price", "quantity"}

def _apply_map(tables: dict[str, pd.DataFrame], valid: set[str], sku: str, ingredient: str, sources: str) -> str | None:
    cat, pur = tables["catalog"], tables["purchases"]
    if sku not in set(cat.sku):
        return f"map ignored: SKU {sku} is not in the catalog"
    if ingredient not in valid:
        return f"map ignored: {ingredient!r} is not a kitchen ingredient. Use one of: {'; '.join(sorted(valid))}"
    for df in (cat, pur):
        df.loc[df.sku == sku, ["ingredient", "overridden_by"]] = [ingredient, sources]
    return None

def _apply_set(tables: dict[str, pd.DataFrame], kind: str, key: str, field: str, value: str,
               start: str, sources: str) -> str | None:
    table, key_col, fields = OVERRIDES[kind]
    df = tables[table]
    if field not in fields:
        return f"set ignored: {kind} field {field!r} can't be overridden. Allowed: {', '.join(sorted(fields))}"
    if key not in set(df[key_col]):
        return f"set ignored: {kind} {key} does not exist"
    if field in NUMERIC_FIELDS:
        try:
            value = float(value)
        except ValueError:
            return f"set ignored: {field} must be a number, got {value!r}"
    label = sources + (f" (from {start})" if start else "")
    rows = df[key_col] == key
    if kind == "vendor" and start:
        # Keep the old value for dates before the change; snapshot reads these through vendor_field().
        df.loc[rows, [f"previous_{field}", f"{field}_from"]] = [df.loc[rows, field].iloc[0], start]
    df.loc[rows, [field, "overridden_by"]] = [value, label]
    if kind == "invoice":
        pur = tables["purchases"]
        pur.loc[pur.invoice_id == key, [field, "overridden_by"]] = [value, label]
    elif kind == "line":
        r = df.loc[rows]
        base_qty = r.quantity.where(r.unit != "case", r.quantity * r.pack_quantity)
        df.loc[rows, "cost"] = r.quantity * r.unit_price
        df.loc[rows, "base_qty"] = base_qty
        df.loc[rows, "price_per_base"] = (df.loc[rows, "cost"] / base_qty).round(4)
    return None

def apply_memory() -> list[str]:
    """Rebuild overridable tables from raw_* and apply '- map' and '- set' lines from memory.md.

    Returns problems to report back to the agent.
    """
    path = config.MEMORY_DIR / "memory.md"
    text = path.read_text() if path.exists() else ""
    with db.connect() as con:
        tables = db.normalize({t: pd.read_sql_query(f"SELECT * FROM {t}", con) for t in db.RAW_FILES})
        valid = kitchen_ingredients(con)
        problems = [_apply_map(tables, valid, *m) for m in MAP_LINE.findall(text)]
        problems += [_apply_set(tables, *m) for m in SET_LINE.findall(text)]
        for name in db.OVERRIDABLE:
            tables[name].to_sql(name, con, index=False, if_exists="replace")
    return [p for p in problems if p]

def sync_open_questions() -> list[str]:
    """Add a question under Open for each new gap; close questions whose gap is gone."""
    with db.connect() as con:
        gaps = {g["key"]: g for g in find_gaps(con)}
    path = config.MEMORY_DIR / "memory.md"
    text = path.read_text()
    changes = []
    for key, gap in gaps.items():
        if TAG.format(key=key) not in text:
            line = f"- [ ] {gap['question']} {TAG.format(key=key)}"
            text = text.replace("### Questions\n", f"### Questions\n{line}\n", 1)
            changes.append(f"opened {key}")
    for key in re.findall(r"^- \[ \] .*\(data check: (\S+)\)$", text, re.M):
        if key not in gaps:
            text = re.sub(rf"^- \[ \] (.*\(data check: {re.escape(key)}\))$", r"- [x] \1 (resolved: mapping applied)",
                          text, flags=re.M)
            changes.append(f"closed {key}")
    if changes:
        path.write_text(text)
    return changes


# Citations. An answer cites in square brackets: row or note IDs [INV-099-L01, N15], a range over one
# table [POS-15-M01 to POS-21-M01], or a snapshot table [snapshot: stock coverage]. Every bracket must
# resolve to one of these; the check can't tell whether the cited rows support the claim.
ID_PATTERN = re.compile(
    r"\b(INV-\d{3}(?:-L\d{2})?|POS-\d{2}-M\d{2}|COUNT-(?:OPEN|CLOSE)-[A-Z]+|REC-M\d{2}-[A-Z]+"
    r"|CAT-[A-Z0-9]+|[NU]\d{2}|M\d{2}|V\d)\b"
)
BRACKET = re.compile(r"\[([^\[\]]+)\]")
RANGE = re.compile(rf"({ID_PATTERN.pattern})\s+to\s+({ID_PATTERN.pattern})")


def citation_problems(answer: str, snapshot_tables: set[str]) -> list[str]:
    tables = db.id_tables()
    problems = []
    for cite in BRACKET.findall(answer):
        cite = cite.strip()
        if cite in ("", "x"):  # markdown checkbox
            continue
        if cite.lower().startswith("snapshot:"):
            if (name := cite.split(":", 1)[1].strip().lower()) not in snapshot_tables:
                problems.append(f"[{cite}]: no snapshot table {name!r}; tables are {', '.join(sorted(snapshot_tables))}")
            continue
        for start, end in ((m[1], m[3]) for m in RANGE.finditer(cite)):
            if start in tables and end in tables and (tables[start] != tables[end] or start > end):
                problems.append(f"[{cite}]: {start} to {end} is not a range within one table")
        ids = ID_PATTERN.findall(cite)
        if unknown := sorted({i for i in ids if i not in tables}):
            problems.append(f"[{cite}]: {', '.join(unknown)} not in the records")
        elif not ids:
            problems.append(f"[{cite}]: names no row, note or snapshot table")
    return problems
