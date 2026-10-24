"""Missing-label checks: records the data can't be interpreted without.

The checks only detect gaps; they never fill them. Gaps become questions under Open in memory.md.
Answers and corrections are recorded under its Data section ('- map ...', '- set ...') and applied to
the normalized tables (raw_* stay untouched), so every later query sees them with an overridden_by source.

Citation checks confirm that every bracket in an answer names a real row, note, range, or snapshot
table. They do not judge whether the cited rows support the claim.
"""

import re
import sqlite3

import pandas as pd

import config
import db

TAG = "(data check: {key})"
OPEN_CHECK = re.compile(r"^- \[ \] .*\(data check: (\S+)\)$", re.M)


def find_gaps(con: sqlite3.Connection) -> list[dict]:
    return _unmapped_sku_gaps(con) + _missing_recipe_gaps(con)


def _unmapped_sku_gaps(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute("""
        SELECT c.sku, c.description, c.vendor_id, c.catalog_row_id, GROUP_CONCAT(p.line_id, ', ')
        FROM catalog c LEFT JOIN purchases p ON p.sku = c.sku
        WHERE c.ingredient IS NULL GROUP BY c.sku""")
    gaps = []
    for sku, desc, vendor, cat_id, lines in rows:
        gaps.append({
            "key": f"sku:{sku}",
            "summary": f"{lines or 'no'} purchase lines have no ingredient (SKU {sku})",
            "question": (
                f"SKU {sku} ({desc!r}, vendor {vendor}) isn't linked to a kitchen ingredient, so its "
                f"purchase lines are missing from ingredient totals [{cat_id}, {lines or 'no lines'}]. "
                f"Which ingredient is it? Record as '- map {sku} -> <ingredient> [sources]' under Data in memory.md."
            ),
        })
    return gaps


def _missing_recipe_gaps(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute("""
        SELECT s.item_id, s.item, GROUP_CONCAT(s.row_id, ', ')
        FROM sales s WHERE s.item_id NOT IN (SELECT item_id FROM recipes) GROUP BY s.item_id""")
    gaps = []
    for item_id, name, row_ids in rows:
        first = row_ids.split(", ")[0]
        gaps.append({
            "key": f"recipe:{item_id}",
            "summary": f"{item_id} {name} sells but has no recipe, so its ingredient use is not counted",
            "question": (
                f"{item_id} {name} has sales but no recipe card [{item_id}, {first} ...]. "
                "What goes into one portion?"
            ),
        })
    return gaps


def kitchen_ingredients(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute("SELECT ingredient FROM counts UNION SELECT ingredient FROM recipes")}


MAP_LINE = re.compile(r"^- map (\S+) -> (.+?)\s*\[(.+?)\]", re.M)
SET_LINE = re.compile(
    r"^- set (vendor|invoice|line) (\S+) (\w+) = (.+?)(?: from (\d{4}-\d{2}-\d{2}))?\s*\[(.+?)\]",
    re.MULTILINE,
)
# What memory may override, and where: kind -> (table, key column, allowed fields).
OVERRIDES = {
    "vendor": ("vendors", "vendor_id", {"delivery_days", "order_cutoff"}),
    "invoice": ("invoices", "invoice_id", {"entered_status"}),
    "line": ("purchases", "line_id", {"unit_price", "quantity"}),
}
NUMERIC_FIELDS = {"unit_price", "quantity"}


def apply_memory() -> list[str]:
    """Rebuild overridable tables from raw_* and apply '- map' and '- set' lines from memory.md.

    Returns problems to report back to the agent.
    """
    path = config.MEMORY_DIR / "memory.md"
    text = path.read_text() if path.exists() else ""
    with db.connect() as con:
        tables = db.normalize({name: pd.read_sql_query(f"SELECT * FROM {name}", con) for name in db.RAW_FILES})
        problems = _apply_memory_lines(tables, text, kitchen_ingredients(con))
        for name in db.OVERRIDABLE:
            tables[name].to_sql(name, con, index=False, if_exists="replace")
    return problems


def _apply_memory_lines(tables: dict[str, pd.DataFrame], text: str, valid: set[str]) -> list[str]:
    problems = []
    for sku, ingredient, sources in MAP_LINE.findall(text):
        problem = _apply_map(tables, valid, sku, ingredient, sources)
        if problem:
            problems.append(problem)
    for kind, key, field, value, start, sources in SET_LINE.findall(text):
        problem = _apply_set(tables, kind, key, field, value, start, sources)
        if problem:
            problems.append(problem)
    return problems


def _apply_map(tables: dict[str, pd.DataFrame], valid: set[str], sku: str, ingredient: str, sources: str) -> str | None:
    catalog, purchases = tables["catalog"], tables["purchases"]
    if sku not in set(catalog.sku):
        return f"map ignored: SKU {sku} is not in the catalog"
    if ingredient not in valid:
        names = "; ".join(sorted(valid))
        return f"map ignored: {ingredient!r} is not a kitchen ingredient. Use one of: {names}"
    for df in (catalog, purchases):
        df.loc[df.sku == sku, ["ingredient", "overridden_by"]] = [ingredient, sources]
    return None


def _apply_set(tables: dict[str, pd.DataFrame], kind: str, key: str, field: str, value: str,
               start: str, sources: str) -> str | None:
    table, key_col, allowed = OVERRIDES[kind]
    df = tables[table]
    if field not in allowed:
        return f"set ignored: {kind} field {field!r} can't be overridden. Allowed: {', '.join(sorted(allowed))}"
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
        previous = df.loc[rows, field].iloc[0]
        df.loc[rows, [f"previous_{field}", f"{field}_from"]] = [previous, start]
    df.loc[rows, [field, "overridden_by"]] = [value, label]

    if kind == "invoice":
        purchases = tables["purchases"]
        purchases.loc[purchases.invoice_id == key, [field, "overridden_by"]] = [value, label]
    elif kind == "line":
        _recompute_line(df, rows)
    return None


def _recompute_line(df: pd.DataFrame, rows: pd.Series) -> None:
    line = df.loc[rows]
    base_qty = line.quantity.where(line.unit != "case", line.quantity * line.pack_quantity)
    df.loc[rows, "cost"] = line.quantity * line.unit_price
    df.loc[rows, "base_qty"] = base_qty
    df.loc[rows, "price_per_base"] = (df.loc[rows, "cost"] / base_qty).round(4)


def sync_open_questions() -> list[str]:
    """Add a question under Open for each new gap; close questions whose gap is gone."""
    with db.connect() as con:
        gaps = {gap["key"]: gap for gap in find_gaps(con)}
    path = config.MEMORY_DIR / "memory.md"
    text = path.read_text()
    text, opened = _open_new_gaps(text, gaps)
    text, closed = _close_resolved_gaps(text, gaps)
    if opened or closed:
        path.write_text(text)
    return opened + closed


def _open_new_gaps(text: str, gaps: dict[str, dict]) -> tuple[str, list[str]]:
    opened = []
    for key, gap in gaps.items():
        tag = TAG.format(key=key)
        if tag in text:
            continue
        line = f"- [ ] {gap['question']} {tag}"
        text = text.replace("### Questions\n", f"### Questions\n{line}\n", 1)
        opened.append(f"opened {key}")
    return text, opened


def _close_resolved_gaps(text: str, gaps: dict[str, dict]) -> tuple[str, list[str]]:
    closed = []
    for key in OPEN_CHECK.findall(text):
        if key in gaps:
            continue
        text = re.sub(
            rf"^- \[ \] (.*\(data check: {re.escape(key)}\))$",
            r"- [x] \1 (resolved: mapping applied)",
            text,
            flags=re.M,
        )
        closed.append(f"closed {key}")
    return text, closed


# Citations. An answer cites in square brackets: row or note IDs [INV-099-L01, N15], a range over one
# table [POS-15-M01 to POS-21-M01], or a snapshot table [snapshot: stock coverage]. Every bracket must
# resolve to one of these; the check can't tell whether the cited rows support the claim.
_ID = (
    r"INV-\d{3}(?:-L\d{2})?"
    r"|POS-\d{2}-M\d{2}"
    r"|COUNT-(?:OPEN|CLOSE)-[A-Z]+"
    r"|REC-M\d{2}-[A-Z]+"
    r"|CAT-[A-Z0-9]+"
    r"|[NU]\d{2}"
    r"|M\d{2}"
    r"|V\d"
)
ID_PATTERN = re.compile(rf"\b({_ID})\b")
BRACKET = re.compile(r"\[([^\[\]]+)\]")
RANGE = re.compile(rf"\b({_ID})\b\s+to\s+\b({_ID})\b")


def citation_problems(answer: str, snapshot_tables: set[str]) -> list[str]:
    tables = db.id_tables()
    problems = []
    for cite in BRACKET.findall(answer):
        cite = cite.strip()
        if cite in ("", "x"):  # markdown checkbox
            continue
        problems.extend(_problems_in(cite, tables, snapshot_tables))
    return problems


def _problems_in(cite: str, tables: dict[str, str], snapshot_tables: set[str]) -> list[str]:
    if cite.lower().startswith("snapshot:"):
        return _snapshot_problem(cite, snapshot_tables)
    return _range_problems(cite, tables) + _id_problems(cite, tables)


def _snapshot_problem(cite: str, snapshot_tables: set[str]) -> list[str]:
    name = cite.split(":", 1)[1].strip().lower()
    if name in snapshot_tables:
        return []
    known = ", ".join(sorted(snapshot_tables))
    return [f"[{cite}]: no snapshot table {name!r}; tables are {known}"]


def _range_problems(cite: str, tables: dict[str, str]) -> list[str]:
    problems = []
    for match in RANGE.finditer(cite):
        start, end = match.group(1), match.group(2)
        if start not in tables or end not in tables:
            continue
        if tables[start] != tables[end] or start > end:
            problems.append(f"[{cite}]: {start} to {end} is not a range within one table")
    return problems


def _id_problems(cite: str, tables: dict[str, str]) -> list[str]:
    ids = ID_PATTERN.findall(cite)
    if not ids:
        return [f"[{cite}]: names no row, note or snapshot table"]
    unknown = sorted({i for i in ids if i not in tables})
    if not unknown:
        return []
    return [f"[{cite}]: {', '.join(unknown)} not in the records"]
