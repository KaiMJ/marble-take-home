"""Fact snapshot for the morning brief: fixed queries, plain numbers with row IDs, no flags or ranking.

It gives the brief a starting point so it doesn't depend on the model choosing to run the basics.
Deciding what matters stays with the model.
"""

import sqlite3
from datetime import date, timedelta

import pandas as pd

import config
import db
from checks import find_gaps

END = str(config.PERIOD_END)


def _q(con: sqlite3.Connection, query: str) -> pd.DataFrame:
    return pd.read_sql_query(query, con)


def _table(df: pd.DataFrame) -> str:
    return df.to_csv(sep="|", index=False, float_format="%.10g", lineterminator="\n").rstrip("\n")


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


DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def vendor_field(v: pd.Series, field: str, day: date) -> str:
    """A vendor sheet field as of `day`: before a dated override, the value it replaced."""
    start = v.get(f"{field}_from")
    if pd.notna(start) and str(day) < start:
        return v[f"previous_{field}"]
    return v[field]


def _delivers(v: pd.Series, day: date) -> bool:
    return DAYS[day.weekday()] in str(vendor_field(v, "delivery_days", day))


def coverage(con: sqlite3.Connection) -> str:
    """Stock against the next two deliveries, from average expected use by weekday over the last 14 days."""
    today = config.PERIOD_END + timedelta(days=1)
    use = expected_use(con)
    recent = use[use.date > str(config.PERIOD_END - timedelta(days=14))]
    per_day = recent.groupby(["ingredient", "weekday"]).use.sum().div(
        recent.groupby("weekday").date.nunique(), level="weekday")
    vendors = _q(con, "SELECT * FROM vendors").set_index("vendor_id")
    supplier = _q(con, "SELECT DISTINCT ingredient, vendor_id FROM catalog WHERE ingredient IS NOT NULL")
    inbound = _q(con, f"""SELECT ingredient, delivery_date, base_qty, line_id FROM purchases
                          WHERE delivery_date > '{END}' AND ingredient IS NOT NULL""")
    counts = _q(con, "SELECT ingredient, quantity, row_id FROM counts WHERE row_id LIKE 'COUNT-CLOSE%'")
    rows = []
    for c in counts.itertuples():
        v = vendors.loc[supplier[supplier.ingredient == c.ingredient].vendor_id.iloc[0]]
        days = [today + timedelta(days=i) for i in range(21)]
        daily = {d: per_day.get((c.ingredient, d.strftime("%a")), 0.0) for d in days}
        arrivals = inbound[inbound.ingredient == c.ingredient]
        # Today's order cutoff has passed, so the next delivery that can still be ordered is from tomorrow.
        nxt, after = [d for d in days[1:] if _delivers(v, d)][:2]
        stock, runs_out = c.quantity, None
        for d in days:
            stock += arrivals[arrivals.delivery_date == str(d)].base_qty.sum()
            stock -= daily[d]
            if stock < 0 and runs_out is None:
                runs_out = d
        before = sum(daily[d] for d in days if d < nxt)
        cycle = sum(daily[d] for d in days if nxt <= d < after)
        arriving = arrivals[arrivals.delivery_date < str(after)]
        left_at_next = c.quantity + arriving.base_qty.sum() - before
        rows.append({
            "ingredient": c.ingredient, "vendor": v.name, "delivers_today": _delivers(v, today),
            "on_hand": c.quantity,
            "inbound": arriving.base_qty.sum(), "runs_out": f"{runs_out:%a %m-%d}" if runs_out else "> 3 weeks",
            "next_delivery": f"{nxt:%a %m-%d}",
            "order_cutoff": cutoff if pd.notna(cutoff := vendor_field(v, "order_cutoff", nxt)) else "unknown",
            "use_before_next": round(before, 1),
            "short_before_next": round(max(0.0, -left_at_next), 1),
            "delivery_after": f"{after:%a %m-%d}", "use_between": round(cycle, 1),
            "order_for_next": round(max(0.0, cycle - max(0.0, left_at_next)), 1),
            "rows": ", ".join([c.row_id, *arriving.line_id]),
        })
    return _table(pd.DataFrame(rows))


def anomalies(con: sqlite3.Connection) -> str:
    """Records that fail a mechanical check. A hit is a lead to investigate, not a conclusion."""
    rows = []
    p = _q(con, "SELECT line_id, ingredient, price_per_base, base_unit FROM purchases WHERE ingredient IS NOT NULL")
    median = p.groupby("ingredient").price_per_base.transform("median")
    for r in p[(p.price_per_base > 2 * median) | (p.price_per_base < median / 2)].itertuples():
        rows.append({"record": r.line_id, "check": "price over 2x off the ingredient's median",
                     "detail": f"{r.price_per_base} vs median {median[r.Index]:.4f} per {r.base_unit}"})
    vendors = _q(con, "SELECT * FROM vendors").set_index("account_number")
    invoices = _q(con, """SELECT h.invoice_id, h.vendor_account, h.delivery_date, h.status, h.invoice_total,
                                 h.tax + h.delivery_fee + SUM(l.line_total) AS computed
                          FROM raw_invoice_headers h JOIN raw_invoice_lines l USING (invoice_id)
                          GROUP BY h.invoice_id""")
    for r in invoices.itertuples():
        if abs(r.invoice_total - r.computed) > 0.01:
            rows.append({"record": r.invoice_id, "check": "header total != lines + tax + fee",
                         "detail": f"{r.invoice_total} vs {r.computed:.2f}"})
        if r.delivery_date > END and r.status == "received":
            rows.append({"record": r.invoice_id, "check": "entered received, delivery date after the last count",
                         "detail": f"delivery {r.delivery_date}"})
        day = date.fromisoformat(r.delivery_date)
        if r.vendor_account in vendors.index and not _delivers(vendors.loc[r.vendor_account], day):
            v = vendors.loc[r.vendor_account]
            rows.append({"record": r.invoice_id, "check": "delivery on a day the vendor doesn't deliver",
                         "detail": f"{day:%a %m-%d}; {v.vendor_id} delivers {vendor_field(v, 'delivery_days', day)}"})
    return _table(pd.DataFrame(rows)) if rows else "(none)"


# Table name (what an answer cites as [snapshot: <name>]) -> (description, query).
TABLES = {
    "anomalies": ("mechanical checks; each is a lead, not a conclusion", anomalies),
    "stock coverage": ("from today, weekday-average use; excludes items without a recipe and demand spikes; "
                       "a delivery today counts only if it is entered as inbound", coverage),
    "weekly sales and purchases": (f"spend by delivery date, through {END}", weekly),
    "stock reconciliation": (f"{config.PERIOD_START} to {END}; unexplained = closing - (opening + purchased "
                             "- expected use)", reconciliation),
    "stock vs use": ("on hand vs average expected daily use, last 14 days", stock_vs_use),
    "purchase prices": ("per base unit", price_changes),
    "vendor schedule": ("order sheet, with memory overrides shown in overridden_by", vendor_sheet),
    "memory overrides": ("values changed by the Data section, with the source value", overrides),
    "missing labels": ("rows excluded from the figures above",
                       lambda con: "\n".join(f"- {g['summary']}" for g in find_gaps(con)) or "(none)"),
}


def build() -> str:
    with db.connect() as con:
        return "\n\n".join(f"### {name} ({desc})\n{query(con)}" for name, (desc, query) in TABLES.items())
