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


def connect(read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    return sqlite3.connect(config.DB_PATH)

def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    with connect() as con:
        return pd.read_sql_query(sql, con, params=params)

def _week(d: pd.Series) -> pd.Series:
    return (pd.to_datetime(d) - pd.Timestamp(config.PERIOD_START)).dt.days // 7 + 1


def parse_ops_log(text: str) -> pd.DataFrame:
    pattern = re.compile(r"^### (N\d+) \| (\S+ \S+) \| (\S+)\n\n(.+?)(?=\n### |\Z)", re.M | re.S)
    rows = [
        {"note_id": m[1], "written_at": m[2], "author": m[3], "source": "log", "text": m[4].strip()}
        for m in pattern.finditer(text)
    ]
    return pd.DataFrame(rows)


# Tables rebuilt from raw_* whenever memory overrides change, so removing an override restores the source value.
OVERRIDABLE = ("vendors", "catalog", "invoices", "purchases")


def normalize(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    vendors = raw["raw_vendors"].copy()
    vendor_by_account = dict(zip(vendors.account_number, vendors.vendor_name, strict=True))
    vendors["order_cutoff"] = None
    # Set by a dated memory override: the value before the change and the date it took effect.
    for field in ("delivery_days", "order_cutoff"):
        vendors[f"previous_{field}"] = None
        vendors[f"{field}_from"] = None

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
    lines["week"] = _week(lines.delivery_date)
    purchases = recompute(lines.rename(columns={"row_id": "line_id", "line_total": "cost"}))[
        ["delivery_date", "invoice_date", "week", "vendor", "ingredient", "sku", "item_description",
         "quantity", "unit", "unit_price", "pack_quantity", "base_qty", "base_unit", "cost", "price_per_base",
         "entered_status", "invoice_id", "line_id"]
    ]
    for df in (vendors, catalog, invoices, purchases):
        df["overridden_by"] = None
    return {"vendors": vendors, "catalog": catalog, "invoices": invoices, "purchases": purchases}


def recompute(lines: pd.DataFrame) -> pd.DataFrame:
    """Base-unit quantity and price from quantity, unit, and pack size."""
    lines = lines.copy()
    is_case = lines.unit == "case"
    lines["base_qty"] = lines.quantity.where(~is_case, lines.quantity * lines.pack_quantity)
    lines["base_unit"] = lines.pack_unit.where(is_case, lines.unit)
    lines["price_per_base"] = (lines.cost / lines.base_qty).round(4)
    return lines


def build() -> None:
    config.VAR_DIR.mkdir(exist_ok=True)
    config.DB_PATH.unlink(missing_ok=True)
    raw = {name: pd.read_csv(config.DATA_DIR / f) for name, f in RAW_FILES.items()}

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
        **normalize(raw),
        "sales": sales,
        "recipes": recipes,
        "counts": counts,
        "menu": menu,
        "notes": notes,
    }
    with connect() as con:
        for name, df in tables.items():
            df.to_sql(name, con, index=False)
        ids = pd.concat(
            [pd.DataFrame({"id": tables[t][c], "tbl": t}) for t, c in ID_COLUMNS.items()]
            + [pd.DataFrame({"id": tables["catalog"].sku, "tbl": "catalog"})]
        )
        ids.drop_duplicates("id").to_sql("ids", con, index=False)


def add_chat_note(text: str, written_at: str, author: str = "user") -> str:
    """Record a user chat message as a citable source (U01, U02, ...)."""
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM notes WHERE source = 'chat'").fetchone()[0]
        note_id = f"U{n + 1:02d}"
        con.execute("INSERT INTO notes VALUES (?, ?, ?, 'chat', ?)", (note_id, written_at, author, text))
        con.execute("INSERT INTO ids VALUES (?, 'notes')", (note_id,))
    return note_id


def id_tables() -> dict[str, str]:
    """Every citable ID and the table it belongs to."""
    with connect() as con:
        return dict(con.execute("SELECT id, tbl FROM ids"))
