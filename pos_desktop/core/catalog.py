"""Products, stock and purchasing (with weighted-average costing)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from . import accounting as acc
from .accounting import Line
from .db import Database, now_text, today_text


class CatalogError(ValueError):
    pass


@dataclass
class Product:
    id: int | None = None
    sku: str = ""
    barcode: str = ""
    name: str = ""
    category: str = "General"
    price: int = 0
    cost: int = 0
    stock: int = 0
    track_stock: bool = True
    reorder_level: int = 0
    taxable: bool = True
    is_active: bool = True

    @property
    def is_low(self) -> bool:
        return self.track_stock and self.stock <= self.reorder_level


def _product(row) -> Product:
    return Product(
        id=row["id"], sku=row["sku"], barcode=row["barcode"], name=row["name"],
        category=row["category"], price=row["price"], cost=row["cost"],
        stock=row["stock"], track_stock=bool(row["track_stock"]),
        reorder_level=row["reorder_level"], taxable=bool(row["taxable"]),
        is_active=bool(row["is_active"]),
    )


def list_products(db: Database, search: str = "", category: str | None = None,
                  include_inactive: bool = False, low_stock_only: bool = False
                  ) -> list[Product]:
    sql = "SELECT * FROM products WHERE 1=1"
    params: list = []
    if not include_inactive:
        sql += " AND is_active = 1"
    if category:
        sql += " AND category = ?"
        params.append(category)
    if search:
        sql += " AND (name LIKE ? OR sku LIKE ? OR barcode = ?)"
        params += [f"%{search}%", f"%{search}%", search]
    if low_stock_only:
        sql += " AND track_stock = 1 AND stock <= reorder_level"
    return [_product(r) for r in db.all(sql + " ORDER BY name COLLATE NOCASE", params)]


def get_product(db: Database, product_id: int) -> Product:
    row = db.one("SELECT * FROM products WHERE id = ?", (product_id,))
    if row is None:
        raise CatalogError("Product not found")
    return _product(row)


def find_by_code(db: Database, code: str) -> Product | None:
    code = code.strip()
    if not code:
        return None
    row = db.one(
        "SELECT * FROM products WHERE is_active = 1 AND (barcode = ? OR sku = ? COLLATE NOCASE) "
        "LIMIT 1",
        (code, code),
    )
    return _product(row) if row else None


def categories(db: Database) -> list[str]:
    return [r[0] for r in db.all(
        "SELECT DISTINCT category FROM products WHERE is_active = 1 ORDER BY category COLLATE NOCASE"
    )]


def next_sku(db: Database) -> str:
    n = db.scalar("SELECT COUNT(*) FROM products", default=0) + 1
    while db.scalar("SELECT 1 FROM products WHERE sku = ?", (f"P{n:05d}",)):
        n += 1
    return f"P{n:05d}"


def _validate(db: Database, p: Product) -> None:
    if not p.name.strip():
        raise CatalogError("Name is required")
    if not p.sku.strip():
        raise CatalogError("SKU is required")
    if p.price < 0 or p.cost < 0:
        raise CatalogError("Price and cost can't be negative")
    if db.scalar("SELECT id FROM products WHERE sku = ? AND id IS NOT ?", (p.sku.strip(), p.id)):
        raise CatalogError(f"SKU {p.sku} is already used")
    if p.barcode.strip() and db.scalar(
        "SELECT id FROM products WHERE barcode = ? AND id IS NOT ?", (p.barcode.strip(), p.id)
    ):
        raise CatalogError(f"Barcode {p.barcode} is already used")


def create_product(db: Database, p: Product, opening_stock: int = 0,
                   date: str | None = None) -> int:
    """Creates a product. Opening stock is valued at cost and credited to
    Opening Balance Equity."""
    _validate(db, p)
    if opening_stock < 0:
        raise CatalogError("Opening stock can't be negative")
    now = now_text()
    with db.transaction():
        pid = db.insert(
            "INSERT INTO products (sku, barcode, name, category, price, cost, stock, "
            "track_stock, reorder_level, taxable, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            (p.sku.strip(), p.barcode.strip(), p.name.strip(),
             p.category.strip() or "General", p.price, p.cost, int(p.track_stock),
             p.reorder_level, int(p.taxable), int(p.is_active), now, now),
        )
        if opening_stock and p.track_stock:
            _move_stock(db, pid, opening_stock, p.cost, "opening", "Opening stock")
            value = opening_stock * p.cost
            if value:
                entry = acc.post(db, f"Opening stock: {p.name}", [
                    Line(acc.account_id(db, "inventory"), debit=value),
                    Line(acc.account_id(db, "opening"), credit=value),
                ], source="opening", source_id=pid, date=date)
                db.execute(
                    "UPDATE stock_movements SET journal_entry_id = ? "
                    "WHERE product_id = ? AND reason = 'opening'", (entry, pid))
    return pid


def update_product(db: Database, p: Product) -> None:
    """Updates details. Stock changes go through purchases/adjustments."""
    if p.id is None:
        raise CatalogError("Product has no id")
    _validate(db, p)
    db.execute(
        "UPDATE products SET sku = ?, barcode = ?, name = ?, category = ?, price = ?, "
        "cost = ?, track_stock = ?, reorder_level = ?, taxable = ?, is_active = ?, "
        "updated_at = ? WHERE id = ?",
        (p.sku.strip(), p.barcode.strip(), p.name.strip(),
         p.category.strip() or "General", p.price, p.cost, int(p.track_stock),
         p.reorder_level, int(p.taxable), int(p.is_active), now_text(), p.id),
    )


def _move_stock(db: Database, product_id: int, quantity: int, unit_cost: int,
                reason: str, reference: str = "", entry_id: int | None = None) -> None:
    db.execute("UPDATE products SET stock = stock + ?, updated_at = ? WHERE id = ?",
               (quantity, now_text(), product_id))
    db.execute(
        "INSERT INTO stock_movements (product_id, created_at, quantity, unit_cost, "
        "reason, reference, journal_entry_id, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (product_id, now_text(), quantity, unit_cost, reason, reference, entry_id, db.user_id),
    )


# ---- purchasing ----------------------------------------------------------------


@dataclass
class PurchaseLine:
    product_id: int
    quantity: int
    unit_cost: int


def record_purchase(db: Database, lines: list[PurchaseLine], pay_account_id: int,
                    supplier_id: int | None = None, reference: str = "",
                    date: str | None = None) -> int:
    """Receives stock. Pay from a money account, or Accounts Payable to buy
    on credit (requires a supplier). Updates weighted-average cost."""
    lines = [l for l in lines if l.quantity]
    if not lines:
        raise CatalogError("Add at least one item")
    if any(l.quantity < 0 or l.unit_cost < 0 for l in lines):
        raise CatalogError("Quantities and costs must be positive")
    ap_id = acc.account_id(db, "ap")
    if pay_account_id == ap_id and supplier_id is None:
        raise CatalogError("Choose a supplier for purchases on credit")
    total = sum(l.quantity * l.unit_cost for l in lines)
    date = date or today_text()
    with db.transaction():
        purchase_id = db.insert(
            "INSERT INTO purchases (date, created_at, supplier_id, reference, total, "
            "pay_account_id, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, now_text(), supplier_id, reference, total, pay_account_id, db.user_id),
        )
        supplier = ""
        if supplier_id:
            supplier = db.scalar("SELECT name FROM suppliers WHERE id = ?", (supplier_id,), "")
        entry = acc.post(
            db,
            f"Purchase #{purchase_id}" + (f" from {supplier}" if supplier else "")
            + (f" ({reference})" if reference else ""),
            [Line(acc.account_id(db, "inventory"), debit=total),
             Line(pay_account_id, credit=total)],
            source="purchase", source_id=purchase_id, date=date,
        )
        db.execute("UPDATE purchases SET journal_entry_id = ? WHERE id = ?", (entry, purchase_id))
        for l in lines:
            product = get_product(db, l.product_id)
            db.execute(
                "INSERT INTO purchase_items (purchase_id, product_id, quantity, unit_cost) "
                "VALUES (?, ?, ?, ?)", (purchase_id, l.product_id, l.quantity, l.unit_cost))
            on_hand = max(product.stock, 0)
            new_cost = l.unit_cost if on_hand == 0 else (
                (on_hand * product.cost + l.quantity * l.unit_cost + (on_hand + l.quantity) // 2)
                // (on_hand + l.quantity)
            )
            db.execute("UPDATE products SET cost = ? WHERE id = ?", (new_cost, l.product_id))
            _move_stock(db, l.product_id, l.quantity, l.unit_cost, "purchase",
                        f"Purchase #{purchase_id}", entry)
    return purchase_id


def list_purchases(db: Database, start: str, end: str) -> list[dict]:
    return [dict(r) for r in db.all(
        "SELECT p.*, s.name AS supplier, a.name AS paid_from, "
        "(SELECT COUNT(*) FROM purchase_items i WHERE i.purchase_id = p.id) AS item_count "
        "FROM purchases p LEFT JOIN suppliers s ON s.id = p.supplier_id "
        "JOIN accounts a ON a.id = p.pay_account_id "
        "WHERE p.date >= ? AND p.date <= ? ORDER BY p.date DESC, p.id DESC",
        (start, end),
    )]


def purchase_items(db: Database, purchase_id: int) -> list[dict]:
    return [dict(r) for r in db.all(
        "SELECT i.*, p.name, p.sku FROM purchase_items i JOIN products p ON p.id = i.product_id "
        "WHERE i.purchase_id = ?", (purchase_id,))]


# ---- adjustments -----------------------------------------------------------------


def adjust_stock(db: Database, product_id: int, counted: int, reason: str = "Stock count") -> int:
    """Sets stock to a counted quantity; the value difference is booked to
    Inventory Shrinkage. Returns the quantity change."""
    product = get_product(db, product_id)
    if not product.track_stock:
        raise CatalogError("This product doesn't track stock")
    delta = counted - product.stock
    if delta == 0:
        return 0
    value = abs(delta) * product.cost
    with db.transaction():
        entry = None
        if value:
            inventory, shrinkage = acc.account_id(db, "inventory"), acc.account_id(db, "shrinkage")
            lines = (
                [Line(inventory, debit=value), Line(shrinkage, credit=value)] if delta > 0
                else [Line(shrinkage, debit=value), Line(inventory, credit=value)]
            )
            entry = acc.post(db, f"Stock adjustment: {product.name} ({delta:+d}) – {reason}",
                             lines, source="adjustment", source_id=product_id)
        _move_stock(db, product_id, delta, product.cost, "adjustment", reason, entry)
    return delta


def stock_movements(db: Database, start: str, end: str, product_id: int | None = None
                    ) -> list[dict]:
    sql = (
        "SELECT m.*, p.name, p.sku, u.full_name AS user_name FROM stock_movements m "
        "JOIN products p ON p.id = m.product_id LEFT JOIN users u ON u.id = m.user_id "
        "WHERE substr(m.created_at, 1, 10) >= ? AND substr(m.created_at, 1, 10) <= ?"
    )
    params: list = [start, end]
    if product_id:
        sql += " AND m.product_id = ?"
        params.append(product_id)
    return [dict(r) for r in db.all(sql + " ORDER BY m.id DESC", params)]


def inventory_valuation(db: Database) -> list[dict]:
    return [dict(r) for r in db.all(
        "SELECT id, sku, name, category, stock, cost, price, reorder_level, "
        "stock * cost AS value, stock * price AS retail_value FROM products "
        "WHERE is_active = 1 AND track_stock = 1 ORDER BY category, name"
    )]


# ---- CSV import / export -----------------------------------------------------------

CSV_FIELDS = ["sku", "barcode", "name", "category", "price", "cost", "stock",
              "reorder_level", "taxable", "track_stock", "active"]


def export_products_csv(db: Database, path: str | Path, fmt) -> int:
    products = list_products(db, include_inactive=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        for p in products:
            w.writerow([p.sku, p.barcode, p.name, p.category, fmt.to_input(p.price),
                        fmt.to_input(p.cost), p.stock, p.reorder_level, int(p.taxable),
                        int(p.track_stock), int(p.is_active)])
    return len(products)


def import_products_csv(db: Database, path: str | Path, fmt) -> tuple[int, int, list[str]]:
    """Creates or updates products by SKU. Stock for new products is booked
    as opening stock; stock for existing ones is ignored (use a stock count).
    Returns (created, updated, errors)."""
    created = updated = 0
    errors: list[str] = []

    def flag(value: str, default: bool) -> bool:
        return default if value.strip() == "" else value.strip().lower() in ("1", "true", "yes", "y")

    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    with db.transaction():
        for n, row in enumerate(rows, start=2):
            row = {k.strip().lower(): (v or "") for k, v in row.items() if k}
            try:
                sku = row.get("sku", "").strip() or next_sku(db)
                price = fmt.parse(row.get("price", "0") or "0")
                cost = fmt.parse(row.get("cost", "0") or "0")
                if price is None or cost is None:
                    raise CatalogError("invalid price or cost")
                existing = db.one("SELECT id FROM products WHERE sku = ?", (sku,))
                product = Product(
                    id=existing["id"] if existing else None, sku=sku,
                    barcode=row.get("barcode", ""), name=row.get("name", ""),
                    category=row.get("category", "") or "General", price=price, cost=cost,
                    reorder_level=int(row.get("reorder_level") or 0),
                    taxable=flag(row.get("taxable", ""), True),
                    track_stock=flag(row.get("track_stock", ""), True),
                    is_active=flag(row.get("active", ""), True),
                )
                if existing:
                    current = get_product(db, existing["id"])
                    product.stock = current.stock
                    update_product(db, product)
                    updated += 1
                else:
                    create_product(db, product, int(row.get("stock") or 0))
                    created += 1
            except (ValueError, CatalogError) as e:
                errors.append(f"Row {n}: {e}")
    return created, updated, errors
