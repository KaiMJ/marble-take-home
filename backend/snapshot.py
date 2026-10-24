"""Fact snapshot for the morning brief: fixed queries, plain numbers with row IDs, no flags or ranking.

It gives the brief a starting point so it doesn't depend on the model choosing to run the basics.
Deciding what matters stays with the model.
"""

import sqlite3
from datetime import timedelta

import pandas as pd

import config
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
    use = expected_use(con).groupby("ingredient").use.sum()
    rows = []
    for ing in opening.index:
        purchased = float(bought.set_index("ingredient").purchased.get(ing, 0))
        expected_close = opening.quantity[ing] + purchased - use.get(ing, 0)
        rows.append({
            "ingredient": ing, "unit": opening.unit[ing], "opening": opening.quantity[ing],
            "purchased": purchased, "expected_use": round(use.get(ing, 0), 1),
            "closing": closing.quantity[ing], "unexplained": round(closing.quantity[ing] - expected_close, 1),
            "count_rows": f"{opening.row_id[ing]}, {closing.row_id[ing]}",
        })
    return _table(pd.DataFrame(rows))


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
    df = _q(con, "SELECT sku, ingredient, delivery_date, price_per_base, base_unit, line_id FROM purchases "
                 "ORDER BY sku, delivery_date")
    df["previous"] = df.groupby("sku").price_per_base.shift()
    changed = df[df.previous.notna() & (df.price_per_base != df.previous)]
    firsts = df.groupby("sku").head(1)
    lines = ["Price per base unit when it differs from the SKU's previous purchase:",
             _table(changed[["sku", "ingredient", "delivery_date", "previous", "price_per_base", "base_unit", "line_id"]])
             if len(changed) else "(none)",
             "\nFirst purchase price per SKU:",
             _table(firsts[["sku", "ingredient", "delivery_date", "price_per_base", "base_unit", "line_id"]])]
    return "\n".join(lines)


def vendor_sheet(con: sqlite3.Connection) -> str:
    return _table(_q(con, "SELECT vendor_id, vendor_name, delivery_days, last_reviewed FROM vendors"))


def build() -> str:
    with sqlite3.connect(config.DB_PATH) as con:
        sections = {
            f"Weekly sales and purchases (spend by delivery date, through {END})": weekly(con),
            f"Stock reconciliation {config.PERIOD_START} to {END} (unexplained = closing - (opening + purchased - expected use))":
                reconciliation(con),
            "Stock on hand vs average expected daily use, last 14 days": stock_vs_use(con),
            "Purchase prices": price_changes(con),
            "Vendor order sheet as supplied (may be superseded by memory)": vendor_sheet(con),
            "Missing labels (rows excluded from the figures above)":
                "\n".join(f"- {g['summary']}" for g in find_gaps(con)) or "(none)",
        }
    return "\n\n".join(f"### {title}\n{body}" for title, body in sections.items())
