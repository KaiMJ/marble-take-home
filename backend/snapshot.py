"""Fact snapshot for the morning brief: fixed queries, plain numbers with row IDs, no flags or ranking.

It gives the brief a starting point so it doesn't depend on the model choosing to run the basics.
Deciding what matters stays with the model.
"""

import sqlite3
from datetime import timedelta

import pandas as pd

import config
import db
from checks import find_gaps

END = str(config.PERIOD_END)


def _q(con: sqlite3.Connection, query: str) -> pd.DataFrame:
    return pd.read_sql_query(query, con)


def _table(df: pd.DataFrame) -> str:
    return df.to_string(index=False)


def expected_use(con: sqlite3.Connection) -> pd.DataFrame:
    """Recipe x sales per ingredient per day. Recipes apply from their effective date."""
    return _q(con, """
        SELECT s.date, s.weekday, r.ingredient, SUM(s.qty * r.quantity / r.yield_portions) AS use
        FROM sales s JOIN recipes r ON r.item_id = s.item_id AND r.effective_from <= s.date
        GROUP BY s.date, r.ingredient""")


def weekly(con: sqlite3.Connection) -> str:
    sales = _q(con, "SELECT week, MIN(date) AS start, SUM(net_sales) AS net_sales FROM sales GROUP BY week")
    spend = _q(con, f"""SELECT week, SUM(cost) AS spend, GROUP_CONCAT(DISTINCT invoice_id) AS invoices
                        FROM purchases WHERE delivery_date <= '{END}' GROUP BY week""")
    df = sales.merge(spend, on="week", how="left")
    df["spend_pct_sales"] = (100 * df.spend / df.net_sales).round(1)
    return _table(df[["week", "start", "net_sales", "spend", "spend_pct_sales", "invoices"]])


def reconciliation(con: sqlite3.Connection) -> str:
    counts = _q(con, "SELECT row_id, ingredient, quantity, unit FROM counts")
    opening = counts[counts.row_id.str.startswith("COUNT-OPEN")].set_index("ingredient")
    closing = counts[counts.row_id.str.startswith("COUNT-CLOSE")].set_index("ingredient")
    bought = _q(con, f"""SELECT ingredient, SUM(base_qty) AS purchased FROM purchases
                         WHERE delivery_date <= '{END}' AND ingredient IS NOT NULL GROUP BY ingredient""")
    use = expected_use(con).groupby("ingredient").use.sum().reindex(opening.index, fill_value=0)
    purchased = bought.set_index("ingredient").purchased.reindex(opening.index, fill_value=0).astype(float)
    closing = closing.reindex(opening.index)
    df = pd.DataFrame({
        "unit": opening.unit, "opening": opening.quantity, "purchased": purchased,
        "expected_use": use.round(1), "closing": closing.quantity,
        "unexplained": (closing.quantity - (opening.quantity + purchased - use)).round(1),
        "count_rows": opening.row_id + ", " + closing.row_id,
    })
    return _table(df.reset_index())

def stock_vs_use(con: sqlite3.Connection) -> str:
    """On hand at last count vs average expected use over the last two weeks, by day type."""
    use = expected_use(con)
    recent = use[use.date > str(config.PERIOD_END - timedelta(days=14))].copy()
    recent["day_type"] = recent.weekday.map(lambda d: "weekend" if d in ("Sat", "Sun") else "weekday")
    days = recent.groupby("day_type").date.nunique()
    avg = (recent.groupby(["ingredient", "day_type"]).use.sum() / days).unstack().round(1)
    closing = _q(con, "SELECT ingredient, quantity AS on_hand, row_id FROM counts WHERE row_id LIKE 'COUNT-CLOSE%'")
    inbound = _q(con, f"""SELECT ingredient, SUM(base_qty) AS entered_not_yet_delivered,
                                 GROUP_CONCAT(line_id) AS inbound_lines
                          FROM purchases WHERE delivery_date > '{END}' GROUP BY ingredient""")
    df = closing.merge(avg.rename(columns={"weekday": "avg_weekday_use", "weekend": "avg_weekend_day_use"}),
                       left_on="ingredient", right_index=True, how="left").merge(inbound, on="ingredient", how="left")
    return _table(df.fillna("")[["ingredient", "on_hand", "avg_weekday_use", "avg_weekend_day_use",
                                 "entered_not_yet_delivered", "inbound_lines", "row_id"]])


def price_changes(con: sqlite3.Connection) -> str:
    df = _q(con, "SELECT sku, ingredient, delivery_date, price_per_base, base_unit, line_id, overridden_by "
                 "FROM purchases ORDER BY sku, delivery_date")
    df["previous"] = df.groupby("sku").price_per_base.shift()
    changed = df[df.previous.notna() & (df.price_per_base != df.previous)]
    firsts = df.groupby("sku").head(1)
    lines = ["Price per base unit when it differs from the SKU's previous purchase:",
             _table(changed[["sku", "ingredient", "delivery_date", "previous", "price_per_base", "base_unit", "line_id",
                             "overridden_by"]].fillna(""))
             if len(changed) else "(none)",
             "\nFirst purchase price per SKU:",
             _table(firsts[["sku", "ingredient", "delivery_date", "price_per_base", "base_unit", "line_id"]])]
    return "\n".join(lines)


def overrides(con: sqlite3.Connection) -> str:
    """Values memory changed, with the source value, so corrections stay visible after they're applied."""
    rows = []
    for _, r in _q(con, """SELECT p.line_id AS id, p.unit_price, l.unit_price AS source_unit_price, p.quantity,
                                  l.quantity AS source_quantity, p.ingredient, p.entered_status, p.overridden_by
                           FROM purchases p JOIN raw_invoice_lines l ON l.row_id = p.line_id
                           WHERE p.overridden_by IS NOT NULL""").iterrows():
        changes = [f"{f} {r['source_' + f]} -> {r[f]}" for f in ("unit_price", "quantity") if r[f] != r["source_" + f]]
        rows.append({"record": r.id, "change": "; ".join(changes) or f"ingredient={r.ingredient}, "
                     f"status={r.entered_status}", "sources": r.overridden_by})
    for _, r in _q(con, """SELECT i.invoice_id, h.status, i.entered_status, i.overridden_by FROM invoices i
                           JOIN raw_invoice_headers h USING (invoice_id) WHERE i.overridden_by IS NOT NULL""").iterrows():
        rows.append({"record": r.invoice_id, "change": f"entered_status {r.status} -> {r.entered_status}",
                     "sources": r.overridden_by})
    for _, r in _q(con, """SELECT v.vendor_id, w.delivery_days AS source_days, v.delivery_days, v.order_cutoff,
                                  v.overridden_by FROM vendors v JOIN raw_vendors w USING (vendor_id)
                           WHERE v.overridden_by IS NOT NULL""").iterrows():
        cutoff = f"; order_cutoff {r.order_cutoff}" if pd.notna(r.order_cutoff) else ""
        rows.append({"record": r.vendor_id, "change": f"delivery_days {r.source_days} -> {r.delivery_days}{cutoff}",
                     "sources": r.overridden_by})
    return _table(pd.DataFrame(rows)) if rows else "(none)"


def vendor_sheet(con: sqlite3.Connection) -> str:
    df = _q(con, "SELECT vendor_id, vendor_name, delivery_days, order_cutoff, last_reviewed, overridden_by FROM vendors")
    return _table(df.fillna(""))


def build() -> str:
    with db.connect() as con:
        sections = {
            f"Weekly sales and purchases (spend by delivery date, through {END})": weekly(con),
            f"Stock reconciliation {config.PERIOD_START} to {END} (unexplained = closing - (opening + purchased - expected use))":
                reconciliation(con),
            "Stock on hand vs average expected daily use, last 14 days": stock_vs_use(con),
            "Purchase prices": price_changes(con),
            "Vendor schedule (order sheet, with memory overrides shown in overridden_by)": vendor_sheet(con),
            "Values overridden by memory (data.md), with the source value": overrides(con),
            "Missing labels (rows excluded from the figures above)":
                "\n".join(f"- {g['summary']}" for g in find_gaps(con)) or "(none)",
        }
    return "\n\n".join(f"### {title}\n{body}" for title, body in sections.items())
