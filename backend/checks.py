"""Missing-label checks: records the data can't be interpreted without.

The checks only detect gaps; they never fill them. Gaps become questions in /memories/open.md.
An answer is recorded in /memories/data.md as '- map <SKU> -> <ingredient> [sources]' and
applied to the database, so every later query sees it.
"""

import re
import sqlite3

import config

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


def apply_mappings() -> list[str]:
    """Apply '- map SKU -> ingredient' lines from data.md. Returns problems to report back to the agent."""
    path = config.MEMORY_DIR / "data.md"
    text = path.read_text() if path.exists() else ""
    problems = []
    with sqlite3.connect(config.DB_PATH) as con:
        valid = kitchen_ingredients(con)
        skus = {r[0] for r in con.execute("SELECT sku FROM catalog")}
        for sku, ingredient, _ in MAP_LINE.findall(text):
            if sku not in skus:
                problems.append(f"mapping ignored: SKU {sku} is not in the catalog")
            elif ingredient not in valid:
                problems.append(f"mapping ignored: {ingredient!r} is not a kitchen ingredient. "
                                f"Use one of: {'; '.join(sorted(valid))}")
            else:
                con.execute("UPDATE catalog SET ingredient = ? WHERE sku = ?", (ingredient, sku))
                con.execute("UPDATE purchases SET ingredient = ? WHERE sku = ?", (ingredient, sku))
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
