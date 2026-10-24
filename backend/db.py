"""Load the packet into SQLite: raw_* copies, normalized tables, and an ID index.

Normalization is limited to mechanical work (units, SKU -> ingredient, vendor names).
Anything that needs judgment is left to the agent.
"""

import re
import sqlite3

import pandas as pd

import config

RAW_FILES = {
    "raw_invoice_headers": "invoice_headers.csv",
    "raw_invoice_lines": "invoice_lines.csv",
    "raw_pos_daily": "pos_daily.csv",
    "raw_inventory_counts": "inventory_counts.csv",
    "raw_recipes": "recipes.csv",
    "raw_menu": "menu.csv",
    "raw_vendors": "vendors.csv",
    "raw_vendor_catalog": "vendor_catalog.csv",
}

# Primary ID column per normalized table; used by find() and the citation check.
ID_COLUMNS = {
    "purchases": "line_id",
    "invoices": "invoice_id",
    "sales": "row_id",
    "recipes": "row_id",
    "counts": "row_id",
    "menu": "item_id",
    "vendors": "vendor_id",
    "catalog": "catalog_row_id",
    "notes": "note_id",
}


def _week(d: pd.Series) -> pd.Series:
    return (pd.to_datetime(d) - pd.Timestamp(config.PERIOD_START)).dt.days // 7 + 1


def parse_ops_log(text: str) -> pd.DataFrame:
    pattern = re.compile(r"^### (N\d+) \| (\S+ \S+) \| (\S+)\n\n(.+?)(?=\n### |\Z)", re.M | re.S)
    rows = [
        {"note_id": m[1], "written_at": m[2], "author": m[3], "source": "log", "text": m[4].strip()}
        for m in pattern.finditer(text)
    ]
    return pd.DataFrame(rows)


def build() -> None:
    config.VAR_DIR.mkdir(exist_ok=True)
    config.DB_PATH.unlink(missing_ok=True)
    raw = {name: pd.read_csv(config.DATA_DIR / f) for name, f in RAW_FILES.items()}

    vendors = raw["raw_vendors"]
    vendor_by_account = dict(zip(vendors.account_number, vendors.vendor_name))

    # Map a SKU to a kitchen ingredient only on an exact name match. Anything else stays
    # unmapped (NULL) for the agent to resolve with evidence; pack size still comes from the catalog.
    kitchen_names = set(raw["raw_inventory_counts"].ingredient_name) | set(raw["raw_recipes"].ingredient_name)
    catalog = raw["raw_vendor_catalog"].copy()
    catalog["ingredient"] = catalog.description.where(catalog.description.isin(kitchen_names))

    invoices = raw["raw_invoice_headers"].rename(columns={"status": "entered_status"})
    invoices["vendor"] = invoices.vendor_account.map(vendor_by_account).fillna(invoices.vendor_name)
    invoices = invoices.rename(columns={"vendor_name": "vendor_name_as_entered"})

    lines = raw["raw_invoice_lines"].merge(
        invoices[["invoice_id", "vendor", "invoice_date", "delivery_date", "entered_status"]], on="invoice_id"
    )
    lines = lines.merge(catalog[["sku", "ingredient", "pack_quantity", "pack_unit"]], on="sku", how="left")
    is_case = lines.unit == "case"
    lines["base_qty"] = lines.quantity.where(~is_case, lines.quantity * lines.pack_quantity)
    lines["base_unit"] = lines.pack_unit.where(is_case, lines.unit)
    lines["price_per_base"] = (lines.line_total / lines.base_qty).round(4)
    lines["week"] = _week(lines.delivery_date)
    purchases = lines.rename(columns={"row_id": "line_id", "line_total": "cost"})[
        ["delivery_date", "invoice_date", "week", "vendor", "ingredient", "sku", "item_description",
         "quantity", "unit", "unit_price", "base_qty", "base_unit", "cost", "price_per_base",
         "entered_status", "invoice_id", "line_id"]
    ]

    sales = raw["raw_pos_daily"].rename(columns={"business_date": "date", "pos_item_id": "item_id",
                                                 "item_name": "item", "quantity_sold": "qty"})
    sales.insert(1, "weekday", pd.to_datetime(sales.date).dt.day_name().str[:3])
    sales.insert(2, "week", _week(sales.date))

    recipes = raw["raw_recipes"].rename(columns={"pos_item_id": "item_id", "ingredient_name": "ingredient"})
    counts = raw["raw_inventory_counts"].rename(columns={"ingredient_name": "ingredient"})
    menu = raw["raw_menu"].rename(columns={"pos_item_id": "item_id"})
    notes = parse_ops_log(config.OPS_LOG.read_text())

    tables = {
        **raw,
        "purchases": purchases,
        "invoices": invoices,
        "sales": sales,
        "recipes": recipes,
        "counts": counts,
        "menu": menu,
        "vendors": vendors,
        "catalog": catalog,
        "notes": notes,
    }
    with sqlite3.connect(config.DB_PATH) as con:
        for name, df in tables.items():
            df.to_sql(name, con, index=False)
        ids = pd.concat(
            [pd.DataFrame({"id": tables[t][c], "tbl": t}) for t, c in ID_COLUMNS.items()]
            + [pd.DataFrame({"id": catalog.sku, "tbl": "catalog"})]
        )
        ids.drop_duplicates("id").to_sql("ids", con, index=False)


def add_chat_note(text: str, written_at: str, author: str = "user") -> str:
    """Record a user chat message as a citable source (U01, U02, ...)."""
    with sqlite3.connect(config.DB_PATH) as con:
        n = con.execute("SELECT COUNT(*) FROM notes WHERE source = 'chat'").fetchone()[0]
        note_id = f"U{n + 1:02d}"
        con.execute("INSERT INTO notes VALUES (?, ?, ?, 'chat', ?)", (note_id, written_at, author, text))
        con.execute("INSERT INTO ids VALUES (?, 'notes')", (note_id,))
    return note_id


def known_ids() -> set[str]:
    with sqlite3.connect(config.DB_PATH) as con:
        return {r[0] for r in con.execute("SELECT id FROM ids")}
