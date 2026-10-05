"""Customer orders, either delivered to the customer or picked up by them at
the branch (`fulfillment`). Orders arrive through the web app's order API (or
are entered by an admin), wait as "new", are taken by a staff member, and end
up completed or cancelled:

    new ──take──▶ picked_up ──complete──▶ completed
     ▲                │
     └──── release ───┘        new / picked_up ──cancel──▶ cancelled

"picked_up" means a staff member has taken the order to prepare it, not that
the customer collected it; screens show it as "In progress".

Items keep a copy of the product's name, SKU and price when the order is
received, so later catalog changes don't alter the order.

Same behavior as web/server/src/core/orders.ts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from . import auth
from .db import Database, now_text


class OrderError(ValueError):
    pass


ORDER_STATUSES = ("new", "picked_up", "completed", "cancelled")
STATUS_LABELS = {"new": "New", "picked_up": "In progress", "completed": "Completed",
                 "cancelled": "Cancelled"}
FULFILLMENTS = {"delivery": "Delivery", "pickup": "Pickup"}
MAX_ITEMS = 200

_LOCAL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$")


@dataclass
class OrderItemInput:
    quantity: int
    product_id: int | None = None
    sku: str | None = None


@dataclass
class OrderInput:
    customer_name: str
    address: str
    phone: str
    scheduled_date: str  # YYYY-MM-DD: the delivery date, or the date the customer collects
    items: list[OrderItemInput] = field(default_factory=list)
    reference: str | None = None
    fulfillment: str | None = None  # "delivery" (default) or "pickup"
    notes: str = ""
    ordered_at: str | None = None  # local "YYYY-MM-DD HH:MM:SS"; defaults to now


def parse_timestamp(text: str) -> str | None:
    """ISO 8601 (with or without time zone) or local "YYYY-MM-DD HH:MM[:SS]",
    as local "YYYY-MM-DD HH:MM:SS". None if unparseable."""
    t = text.strip()
    m = _LOCAL_RE.match(t)
    try:
        if m:
            y, mo, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
            dt = datetime(y, mo, d, h, mi, s)
        elif _ISO_RE.match(t):
            dt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        else:
            return None
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _valid_date(text: str) -> bool:
    try:
        return re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) is not None and bool(date.fromisoformat(text))
    except ValueError:
        return False


def _resolve_items(db: Database, items: list[OrderItemInput]) -> list[dict]:
    if not items:
        raise OrderError("An order needs at least one item")
    if len(items) > MAX_ITEMS:
        raise OrderError(f"An order can have at most {MAX_ITEMS} items")
    missing: list[str] = []
    out: list[dict] = []
    for n, item in enumerate(items, start=1):
        if not isinstance(item.quantity, int) or not 1 <= item.quantity <= 1_000_000:
            raise OrderError(f"Item {n}: quantity must be a whole number of at least 1")
        if item.product_id is not None:
            row = db.one("SELECT * FROM products WHERE id = ?", (item.product_id,))
            if row is None:
                missing.append(f"product_id {item.product_id}")
        elif item.sku and item.sku.strip():
            row = db.one("SELECT * FROM products WHERE sku = ? COLLATE NOCASE", (item.sku.strip(),))
            if row is None:
                missing.append(f"SKU {item.sku.strip()}")
        else:
            raise OrderError(f"Item {n}: give a sku or product_id")
        if row is None:
            continue
        if not row["is_active"]:
            raise OrderError(f"{row['name']} ({row['sku']}) is no longer for sale")
        out.append({"product_id": row["id"], "sku": row["sku"], "name": row["name"],
                    "quantity": item.quantity, "unit_price": row["price"]})
    if missing:
        raise OrderError(f"Unknown products: {', '.join(missing)}")
    return out


def _validate(o: OrderInput) -> tuple[str | None, str, str]:
    fulfillment = o.fulfillment or "delivery"
    if fulfillment not in FULFILLMENTS:
        raise OrderError('fulfillment must be "delivery" or "pickup"')
    if not o.customer_name.strip():
        raise OrderError("Customer name is required")
    if fulfillment == "delivery" and not o.address.strip():
        raise OrderError("Delivery address is required")
    if not o.phone.strip():
        raise OrderError("Phone number is required")
    what = "pickup" if fulfillment == "pickup" else "delivery"
    if not _valid_date(o.scheduled_date):
        raise OrderError(f"Scheduled {what} date must be a date (YYYY-MM-DD)")
    ordered_at = o.ordered_at or now_text()
    if o.scheduled_date < ordered_at[:10]:
        raise OrderError(f"The scheduled {what} date is before the order date")
    return (o.reference or "").strip() or None, ordered_at, fulfillment


def _insert_items(db: Database, order_id: int, items: list[dict]) -> None:
    for it in items:
        db.execute("INSERT INTO order_items (order_id, product_id, sku, name, quantity, unit_price) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (order_id, it["product_id"], it["sku"], it["name"],
                                                 it["quantity"], it["unit_price"]))


def receive_order(db: Database, branch_id: int, o: OrderInput, source: str = "manual"
                  ) -> tuple[int, bool]:
    """Saves an order for `branch_id`. Returns (id, duplicate): with a
    `reference`, sending the same order again returns the existing one."""
    reference, ordered_at, fulfillment = _validate(o)
    with db.transaction():
        if reference:
            existing = db.scalar("SELECT id FROM orders WHERE branch_id = ? AND reference = ?",
                                 (branch_id, reference))
            if existing is not None:
                return existing, True
        items = _resolve_items(db, o.items)
        order_id = db.insert(
            "INSERT INTO orders (branch_id, reference, customer_name, address, phone, scheduled_date, "
            "notes, ordered_at, received_at, source, created_by, fulfillment) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (branch_id, reference, o.customer_name.strip(), o.address.strip(), o.phone.strip(),
             o.scheduled_date, o.notes.strip(), ordered_at, now_text(), source, db.user_id,
             fulfillment))
        _insert_items(db, order_id, items)
        auth.log(db, "order_received", f"Order #{order_id}" + (f" ({reference})" if reference else "")
                 + f" for {o.customer_name.strip()} ({fulfillment}) via {source}")
    return order_id, False


def update_order(db: Database, order_id: int, o: OrderInput) -> None:
    """Admin edit of an open order's details and items."""
    order = get_order(db, order_id)
    if order["status"] in ("completed", "cancelled"):
        raise OrderError(f"Order #{order_id} is {order['status']} and can't be edited")
    o.fulfillment = o.fulfillment or order["fulfillment"]
    o.ordered_at = o.ordered_at or order["ordered_at"]
    reference, ordered_at, fulfillment = _validate(o)
    with db.transaction():
        if reference and db.scalar(
                "SELECT 1 FROM orders WHERE branch_id = ? AND reference = ? AND id <> ?",
                (order["branch_id"], reference, order_id)) is not None:
            raise OrderError(f"Another order already uses reference {reference}")
        items = _resolve_items(db, o.items)
        db.execute(
            "UPDATE orders SET reference = ?, customer_name = ?, address = ?, phone = ?, "
            "scheduled_date = ?, notes = ?, ordered_at = ?, fulfillment = ? WHERE id = ?",
            (reference, o.customer_name.strip(), o.address.strip(), o.phone.strip(),
             o.scheduled_date, o.notes.strip(), ordered_at, fulfillment, order_id))
        db.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        _insert_items(db, order_id, items)
        auth.log(db, "order_updated", f"Order #{order_id}")


def delete_order(db: Database, order_id: int) -> None:
    order = get_order(db, order_id)
    with db.transaction():
        db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        auth.log(db, "order_deleted", f"Order #{order_id} for {order['customer_name']}")


# ---- reading ------------------------------------------------------------------------

_SELECT = (
    "SELECT o.*, b.name AS branch, b.code AS branch_code, b.address AS branch_address, "
    "b.phone AS branch_phone, p.full_name AS picked_by_name, c.full_name AS closed_by_name, "
    "(SELECT COALESCE(SUM(quantity), 0) FROM order_items i WHERE i.order_id = o.id) AS item_count, "
    "(SELECT COALESCE(SUM(quantity * unit_price), 0) FROM order_items i WHERE i.order_id = o.id) AS total "
    "FROM orders o JOIN branches b ON b.id = o.branch_id "
    "LEFT JOIN users p ON p.id = o.picked_by LEFT JOIN users c ON c.id = o.closed_by"
)


def get_order(db: Database, order_id: int) -> dict:
    row = db.one(f"{_SELECT} WHERE o.id = ?", (order_id,))
    if row is None:
        raise OrderError("Order not found")
    items = [dict(r) for r in db.all("SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
                                     (order_id,))]
    return {**dict(row), "items": items}


def list_orders(db: Database, branch_id: int | None, status: str = "open",
                fulfillment: str | None = None, picked_by: int | None = None,
                start: str | None = None, end: str | None = None, search: str = "") -> list[dict]:
    """`branch_id` None means every branch; `status` is a status, "open" or "all";
    `start`/`end` filter the scheduled date."""
    sql = f"{_SELECT} WHERE 1=1"
    params: list = []
    if branch_id is not None:
        sql += " AND o.branch_id = ?"
        params.append(branch_id)
    if status == "open":
        sql += " AND o.status IN ('new', 'picked_up')"
    elif status != "all":
        sql += " AND o.status = ?"
        params.append(status)
    if fulfillment:
        sql += " AND o.fulfillment = ?"
        params.append(fulfillment)
    if picked_by:
        sql += " AND o.picked_by = ?"
        params.append(picked_by)
    if start:
        sql += " AND o.scheduled_date >= ?"
        params.append(start)
    if end:
        sql += " AND o.scheduled_date <= ?"
        params.append(end)
    if search:
        like = f"%{search}%"
        sql += " AND (o.customer_name LIKE ? OR o.phone LIKE ? OR o.reference LIKE ? OR CAST(o.id AS TEXT) = ?)"
        params += [like, like, like, search.lstrip("#")]
    return [dict(r) for r in db.all(sql + " ORDER BY o.scheduled_date, o.ordered_at, o.id LIMIT 1000",
                                    params)]


def count_new(db: Database, branch_id: int | None) -> int:
    if branch_id is None:
        return db.scalar("SELECT COUNT(*) FROM orders WHERE status = 'new'", default=0)
    return db.scalar("SELECT COUNT(*) FROM orders WHERE status = 'new' AND branch_id = ?",
                     (branch_id,), 0)


# ---- workflow ------------------------------------------------------------------------


def pick_up(db: Database, order_id: int) -> None:
    """Assigns a new order to the signed-in staff member. Only one person can take it."""
    if db.user_id is None:
        raise OrderError("Sign in to take orders")
    with db.transaction():
        changed = db.execute(
            "UPDATE orders SET status = 'picked_up', picked_by = ?, picked_at = ? "
            "WHERE id = ? AND status = 'new'", (db.user_id, now_text(), order_id)).rowcount
        if not changed:
            order = get_order(db, order_id)
            if order["status"] == "picked_up":
                raise OrderError(f"Order #{order_id} was already taken by "
                                 f"{order['picked_by_name'] or 'someone else'}")
            raise OrderError(f"Order #{order_id} is {order['status'].replace('_', ' ')}")
        auth.log(db, "order_picked_up", f"Order #{order_id}")


def _require_holder(db: Database, order: dict, manage: bool) -> None:
    if not manage and order["picked_by"] != db.user_id:
        raise OrderError(f"Order #{order['id']} is being handled by "
                         f"{order['picked_by_name'] or 'someone else'}")


def release(db: Database, order_id: int, manage: bool = False) -> None:
    """Puts a taken order back in the queue."""
    with db.transaction():
        order = get_order(db, order_id)
        if order["status"] != "picked_up":
            raise OrderError(f"Order #{order_id} isn't in progress")
        _require_holder(db, order, manage)
        db.execute("UPDATE orders SET status = 'new', picked_by = NULL, picked_at = NULL WHERE id = ?",
                   (order_id,))
        auth.log(db, "order_released", f"Order #{order_id}")


def complete(db: Database, order_id: int, note: str = "", manage: bool = False) -> None:
    with db.transaction():
        order = get_order(db, order_id)
        if order["status"] != "picked_up":
            raise OrderError(f"Take order #{order_id} before completing it")
        _require_holder(db, order, manage)
        db.execute("UPDATE orders SET status = 'completed', closed_by = ?, closed_at = ?, "
                   "status_note = ? WHERE id = ?", (db.user_id, now_text(), note.strip(), order_id))
        auth.log(db, "order_completed", f"Order #{order_id}")


def cancel(db: Database, order_id: int, reason: str, manage: bool = False) -> None:
    if not reason.strip():
        raise OrderError("Give a reason for cancelling")
    with db.transaction():
        order = get_order(db, order_id)
        if order["status"] not in ("new", "picked_up"):
            raise OrderError(f"Order #{order_id} is already {order['status']}")
        if order["status"] == "picked_up":
            _require_holder(db, order, manage)
        elif not manage:
            raise OrderError("Take the order first, or ask an admin to cancel it")
        db.execute("UPDATE orders SET status = 'cancelled', closed_by = ?, closed_at = ?, "
                   "status_note = ? WHERE id = ?", (db.user_id, now_text(), reason.strip(), order_id))
        auth.log(db, "order_cancelled", f"Order #{order_id}: {reason.strip()}")
