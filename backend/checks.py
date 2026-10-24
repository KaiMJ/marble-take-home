"""Missing-label checks: records the data can't be interpreted without.

The checks only detect gaps; they never fill them. Gaps become questions in /memories/open.md.
Answers and corrections are recorded in /memories/data.md ('- map ...', '- set ...') and applied to
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
                         f"Which ingredient is it? Record as '- map {sku} -> <ingredient> [sources]' in /memories/data.md."),
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


def apply_memory() -> list[str]:
    """Rebuild overridable tables from raw_* and apply '- map' and '- set' lines from data.md.

    Returns problems to report back to the agent.
    """
    path = config.MEMORY_DIR / "data.md"
    text = path.read_text() if path.exists() else ""
    problems = []
    with sqlite3.connect(config.DB_PATH) as con:
        raw = {t: pd.read_sql_query(f"SELECT * FROM {t}", con) for t in db.RAW_FILES}
        tables = db.normalize(raw)
        valid = kitchen_ingredients(con)

        for sku, ingredient, sources in MAP_LINE.findall(text):
            cat, pur = tables["catalog"], tables["purchases"]
            if sku not in set(cat.sku):
                problems.append(f"map ignored: SKU {sku} is not in the catalog")
            elif ingredient not in valid:
                problems.append(f"map ignored: {ingredient!r} is not a kitchen ingredient. "
                                f"Use one of: {'; '.join(sorted(valid))}")
            else:
                for df in (cat, pur):
                    df.loc[df.sku == sku, ["ingredient", "overridden_by"]] = [ingredient, sources]

        for kind, key, field, value, start, sources in SET_LINE.findall(text):
            table, key_col, fields = OVERRIDES[kind]
            df = tables[table]
            label = f"{sources}" + (f" (from {start})" if start else "")
            if field not in fields:
                problems.append(f"set ignored: {kind} field {field!r} can't be overridden. "
                                f"Allowed: {', '.join(sorted(fields))}")
            elif key not in set(df[key_col]):
                problems.append(f"set ignored: {kind} {key} does not exist")
            else:
                rows = df[key_col] == key
                if field in ("unit_price", "quantity"):
                    try:
                        value = float(value)
                    except ValueError:
                        problems.append(f"set ignored: {field} must be a number, got {value!r}")
                        continue
                df.loc[rows, [field, "overridden_by"]] = [value, label]
                if kind == "invoice":
                    pur = tables["purchases"]
                    pur.loc[pur.invoice_id == key, [field, "overridden_by"]] = [value, label]
                if kind == "line":
                    r = df.loc[rows]
                    base_qty = r.quantity.where(r.unit != "case", r.quantity * r.pack_quantity)
                    df.loc[rows, "cost"] = r.quantity * r.unit_price
                    df.loc[rows, "base_qty"] = base_qty
                    df.loc[rows, "price_per_base"] = (df.loc[rows, "cost"] / base_qty).round(4)

        for name in db.OVERRIDABLE:
            tables[name].to_sql(name, con, index=False, if_exists="replace")
    return problems


def sync_open_questions() -> list[str]:
    """Add a question to open.md for each new gap; close questions whose gap is gone."""
    with sqlite3.connect(config.DB_PATH) as con:
        gaps = {g["key"]: g for g in find_gaps(con)}
    path = config.MEMORY_DIR / "open.md"
    text = path.read_text()
    changes = []
    for key, gap in gaps.items():
        if TAG.format(key=key) not in text:
            line = f"- [ ] {gap['question']} {TAG.format(key=key)}"
            text = text.replace("## Questions\n", f"## Questions\n{line}\n", 1)
            changes.append(f"opened {key}")
    for line in re.findall(r"^- \[ \] .*\(data check: (\S+)\)$", text, re.M):
        if line not in gaps:
            text = re.sub(rf"^- \[ \] (.*\(data check: {re.escape(line)}\))$", r"- [x] \1 (resolved: mapping applied)",
                          text, flags=re.M)
            changes.append(f"closed {line}")
    if changes:
        path.write_text(text)
    return changes
