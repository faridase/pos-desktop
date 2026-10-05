"""Cart pricing, checkout, refunds and held (parked) orders."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from . import accounting as acc
from . import settings as st
from .accounting import Line
from .catalog import Product, _move_stock, get_product
from .db import Database, now_text, today_text
from .money import allocate, percent_of, round_div

PAYMENT_METHODS = {
    "cash": "Cash",
    "card": "Card",
    "bank": "Bank transfer",
    "account": "On account",
}
METHOD_ACCOUNT = {"cash": "cash", "card": "card", "bank": "bank", "account": "ar"}
# The head branch; a NULL branch_id (older records) means this branch.
HEAD = "(SELECT id FROM branches WHERE is_head = 1)"


class SaleError(ValueError):
    pass


@dataclass
class CartLine:
    product_id: int
    sku: str
    name: str
    unit_price: int
    quantity: int = 1
    discount_bps: int = 0
    taxable: bool = True
    category: str = ""

    @classmethod
    def from_product(cls, p: Product, quantity: int = 1) -> "CartLine":
        return cls(p.id, p.sku, p.name, p.price, quantity, 0, p.taxable, p.category)


@dataclass
class Cart:
    lines: list[CartLine] = field(default_factory=list)
    order_discount: int = 0
    customer_id: int | None = None
    note: str = ""

    def add(self, product: Product, quantity: int = 1) -> CartLine:
        for line in self.lines:
            if line.product_id == product.id and line.unit_price == product.price \
                    and line.discount_bps == 0:
                line.quantity += quantity
                return line
        line = CartLine.from_product(product, quantity)
        self.lines.append(line)
        return line

    @property
    def item_count(self) -> int:
        return sum(l.quantity for l in self.lines)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Cart":
        data = json.loads(text)
        known = CartLine.__dataclass_fields__
        return cls(
            lines=[CartLine(**{k: v for k, v in l.items() if k in known})
                   for l in data.get("lines", [])],
            order_discount=data.get("order_discount", 0),
            customer_id=data.get("customer_id"),
            note=data.get("note", ""),
        )


@dataclass
class PricedLine:
    line: CartLine
    gross: int
    discount: int
    net: int
    tax: int

    @property
    def total(self) -> int:
        return self.net + self.tax


@dataclass
class CartTotals:
    lines: list[PricedLine]
    subtotal: int
    discount: int
    net: int
    tax: int

    @property
    def total(self) -> int:
        return self.net + self.tax


def price_cart(cart: Cart, tax_rate_bps: int) -> CartTotals:
    """Line discounts first, then the order discount is spread across lines
    in proportion to their value, then tax is computed per line."""
    gross = [l.unit_price * l.quantity for l in cart.lines]
    line_discounts = [percent_of(g, l.discount_bps) for g, l in zip(gross, cart.lines)]
    after_line = [g - d for g, d in zip(gross, line_discounts)]
    order_discount = min(max(cart.order_discount, 0), sum(after_line))
    shares = allocate(order_discount, after_line)
    priced = []
    for l, g, ld, a, share in zip(cart.lines, gross, line_discounts, after_line, shares):
        net = a - share
        tax = percent_of(net, tax_rate_bps) if l.taxable else 0
        priced.append(PricedLine(l, g, ld + share, net, tax))
    return CartTotals(
        lines=priced,
        subtotal=sum(gross),
        discount=sum(p.discount for p in priced),
        net=sum(p.net for p in priced),
        tax=sum(p.tax for p in priced),
    )


def checkout(db: Database, cart: Cart, payments: dict[str, int],
             tendered_cash: int | None = None, date: str | None = None,
             approved_by: int | None = None, branch_id: int | None = None) -> int:
    """Completes a sale. `payments` maps method -> amount applied to the sale
    and must add up to the total. For cash, `tendered_cash` may exceed the
    applied amount; the difference is change. `approved_by` records the
    supervisor who authorised a discount or price change."""
    if not cart.lines:
        raise SaleError("The cart is empty")
    if any(l.quantity <= 0 for l in cart.lines):
        raise SaleError("Quantities must be positive")
    totals = price_cart(cart, st.get_int(db, "tax_rate_bps"))
    payments = {m: a for m, a in payments.items() if a}
    for method, amount in payments.items():
        if method not in PAYMENT_METHODS:
            raise SaleError(f"Unknown payment method: {method}")
        if amount < 0:
            raise SaleError("Payment amounts can't be negative")
    if sum(payments.values()) != totals.total:
        raise SaleError("Payments must equal the total")
    if "account" in payments and cart.customer_id is None:
        raise SaleError("Choose a customer to sell on account")
    cash_applied = payments.get("cash", 0)
    tendered = cash_applied if tendered_cash is None else tendered_cash
    if tendered < cash_applied:
        raise SaleError("Cash received is less than the cash amount")
    change = tendered - cash_applied

    products = {l.product_id: get_product(db, l.product_id) for l in cart.lines}
    if not st.get_bool(db, "allow_negative_stock"):
        wanted: dict[int, int] = {}
        for l in cart.lines:
            wanted[l.product_id] = wanted.get(l.product_id, 0) + l.quantity
        for pid, qty in wanted.items():
            p = products[pid]
            if p.track_stock and p.stock < qty:
                raise SaleError(f"Not enough stock for {p.name} ({p.stock} left)")

    date = date or today_text()
    created = now_text() if date == today_text() else f"{date} 12:00:00"
    with db.transaction():
        receipt_no = db.scalar("SELECT MAX(receipt_no) FROM sales", default=0) + 1
        sale_id = db.insert(
            "INSERT INTO sales (receipt_no, created_at, date, customer_id, subtotal, discount, "
            "tax, total, tendered, change_given, note, user_id, approved_by, branch_id) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, {HEAD}))",
            (receipt_no, created, date, cart.customer_id, totals.subtotal, totals.discount,
             totals.tax, totals.total, tendered + sum(v for m, v in payments.items() if m != "cash"),
             change, cart.note, db.user_id, approved_by, branch_id),
        )
        cost_total = 0
        for pl in totals.lines:
            p = products[pl.line.product_id]
            db.execute(
                "INSERT INTO sale_items (sale_id, product_id, sku, name, category, quantity, "
                "unit_price, unit_cost, discount, net, tax, track_stock) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sale_id, p.id, pl.line.sku, pl.line.name, p.category, pl.line.quantity,
                 pl.line.unit_price, p.cost, pl.discount, pl.net, pl.tax, int(p.track_stock)),
            )
            if p.track_stock:
                cost_total += p.cost * pl.line.quantity
        for method, amount in payments.items():
            db.execute("INSERT INTO sale_payments (sale_id, method, amount) VALUES (?, ?, ?)",
                       (sale_id, method, amount))

        lines = [Line(acc.account_id(db, METHOD_ACCOUNT[m]), debit=a) for m, a in payments.items()]
        lines += [
            Line(acc.account_id(db, "sales"), credit=totals.net),
            Line(acc.account_id(db, "tax"), credit=totals.tax),
            Line(acc.account_id(db, "cogs"), debit=cost_total),
            Line(acc.account_id(db, "inventory"), credit=cost_total),
        ]
        entry = None
        if any(l.debit or l.credit for l in lines):
            entry = acc.post(db, f"Sale #{receipt_no}", lines, source="sale",
                             source_id=sale_id, date=date)
            db.execute("UPDATE sales SET journal_entry_id = ? WHERE id = ?", (entry, sale_id))
        for pl in totals.lines:
            p = products[pl.line.product_id]
            if p.track_stock:
                _move_stock(db, p.id, -pl.line.quantity, p.cost, "sale", f"Sale #{receipt_no}", entry)
    return sale_id


# ---- reading sales ----------------------------------------------------------------


def get_sale(db: Database, sale_id: int) -> dict:
    sale = db.one(
        "SELECT s.*, c.name AS customer, u.full_name AS cashier, a.full_name AS approver "
        "FROM sales s LEFT JOIN customers c ON c.id = s.customer_id "
        "LEFT JOIN users u ON u.id = s.user_id LEFT JOIN users a ON a.id = s.approved_by "
        "WHERE s.id = ?", (sale_id,))
    if sale is None:
        raise SaleError("Sale not found")
    items = [dict(r) for r in db.all(
        "SELECT i.*, "
        "COALESCE((SELECT SUM(quantity) FROM refund_items r WHERE r.sale_item_id = i.id), 0) AS refunded_qty, "
        "COALESCE((SELECT SUM(net) FROM refund_items r WHERE r.sale_item_id = i.id), 0) AS refunded_net, "
        "COALESCE((SELECT SUM(tax) FROM refund_items r WHERE r.sale_item_id = i.id), 0) AS refunded_tax "
        "FROM sale_items i WHERE i.sale_id = ? ORDER BY i.id", (sale_id,))]
    payments = [dict(r) for r in db.all(
        "SELECT method, amount FROM sale_payments WHERE sale_id = ? ORDER BY id", (sale_id,))]
    refunds = [dict(r) for r in db.all(
        "SELECT r.*, u.full_name AS user_name, a.full_name AS approver FROM refunds r "
        "LEFT JOIN users u ON u.id = r.user_id LEFT JOIN users a ON a.id = r.approved_by "
        "WHERE r.sale_id = ? ORDER BY r.id", (sale_id,))]
    return {**dict(sale), "items": items, "payments": payments, "refunds": refunds,
            "refunded_total": sum(r["net"] + r["tax"] for r in refunds)}


def list_sales(db: Database, start: str, end: str, search: str = "",
               customer_id: int | None = None) -> list[dict]:
    sql = (
        "SELECT s.*, c.name AS customer, u.full_name AS cashier, b.name AS branch, "
        "(SELECT SUM(quantity) FROM sale_items i WHERE i.sale_id = s.id) AS item_count, "
        "(SELECT GROUP_CONCAT(method, ', ') FROM sale_payments p WHERE p.sale_id = s.id) AS methods, "
        "COALESCE((SELECT SUM(net + tax) FROM refunds r WHERE r.sale_id = s.id), 0) AS refunded "
        "FROM sales s LEFT JOIN customers c ON c.id = s.customer_id "
        "LEFT JOIN users u ON u.id = s.user_id "
        f"LEFT JOIN branches b ON b.id = COALESCE(s.branch_id, {HEAD}) "
        "WHERE s.date >= ? AND s.date <= ?"
    )
    params: list = [start, end]
    if customer_id:
        sql += " AND s.customer_id = ?"
        params.append(customer_id)
    if search:
        sql += " AND (CAST(s.receipt_no AS TEXT) = ? OR c.name LIKE ? OR s.note LIKE ?)"
        params += [search.lstrip("#").lstrip("0") or "0", f"%{search}%", f"%{search}%"]
    return [dict(r) for r in db.all(sql + " ORDER BY s.id DESC", params)]


# ---- refunds -----------------------------------------------------------------------


def refund(db: Database, sale_id: int, quantities: dict[int, int], method: str,
           restock: bool = True, reason: str = "", date: str | None = None,
           approved_by: int | None = None) -> int:
    """Refunds some or all items of a sale. `quantities` maps sale_item_id to
    the quantity being returned."""
    if method not in PAYMENT_METHODS:
        raise SaleError("Invalid refund method")
    sale = get_sale(db, sale_id)
    if method == "account" and not sale["customer_id"]:
        raise SaleError("This sale has no customer account")
    items = {i["id"]: i for i in sale["items"]}
    wanted = {k: v for k, v in quantities.items() if v}
    if not wanted:
        raise SaleError("Choose at least one item to refund")

    parts = []
    for item_id, qty in wanted.items():
        item = items.get(item_id)
        if item is None:
            raise SaleError("Item is not part of this sale")
        remaining = item["quantity"] - item["refunded_qty"]
        if qty < 0 or qty > remaining:
            raise SaleError(f"Can refund at most {remaining} × {item['name']}")
        if qty == remaining:  # settle rounding on the final return
            net = item["net"] - item["refunded_net"]
            tax = item["tax"] - item["refunded_tax"]
        else:
            net = round_div(item["net"] * qty, item["quantity"])
            tax = round_div(item["tax"] * qty, item["quantity"])
        parts.append((item, qty, net, tax))

    net_total = sum(p[2] for p in parts)
    tax_total = sum(p[3] for p in parts)
    cost_total = sum(i["unit_cost"] * q for i, q, _, _ in parts
                     if restock and i["track_stock"] and i["product_id"])
    date = date or today_text()
    with db.transaction():
        refund_id = db.insert(
            "INSERT INTO refunds (sale_id, created_at, date, method, net, tax, restock, reason, "
            "user_id, approved_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sale_id, now_text(), date, method, net_total, tax_total, int(restock), reason,
             db.user_id, approved_by))
        for item, qty, net, tax in parts:
            db.execute(
                "INSERT INTO refund_items (refund_id, sale_item_id, quantity, net, tax) "
                "VALUES (?, ?, ?, ?, ?)", (refund_id, item["id"], qty, net, tax))
        lines = [
            Line(acc.account_id(db, "returns"), debit=net_total),
            Line(acc.account_id(db, "tax"), debit=tax_total),
            Line(acc.account_id(db, METHOD_ACCOUNT[method]), credit=net_total + tax_total),
            Line(acc.account_id(db, "inventory"), debit=cost_total),
            Line(acc.account_id(db, "cogs"), credit=cost_total),
        ]
        entry = None
        if any(l.debit or l.credit for l in lines):
            memo = f"Refund on sale #{sale['receipt_no']}" + (f" – {reason}" if reason else "")
            entry = acc.post(db, memo, lines, source="refund", source_id=refund_id, date=date)
            db.execute("UPDATE refunds SET journal_entry_id = ? WHERE id = ?", (entry, refund_id))
        if restock:
            for item, qty, _, _ in parts:
                if item["track_stock"] and item["product_id"]:
                    _move_stock(db, item["product_id"], qty, item["unit_cost"], "refund",
                                f"Refund on #{sale['receipt_no']}", entry)
        fully = all(
            i["quantity"] - i["refunded_qty"] - wanted.get(i["id"], 0) == 0
            for i in sale["items"]
        )
        db.execute("UPDATE sales SET status = ? WHERE id = ?",
                   ("refunded" if fully else "partially_refunded", sale_id))
    return refund_id


# ---- held orders -------------------------------------------------------------------


def hold_cart(db: Database, cart: Cart, label: str = "", branch_id: int | None = None) -> int:
    if not cart.lines:
        raise SaleError("Nothing to hold")
    return db.insert("INSERT INTO held_carts (created_at, label, data, branch_id) "
                     f"VALUES (?, ?, ?, COALESCE(?, {HEAD}))",
                     (now_text(), label, cart.to_json(), branch_id))


def list_held(db: Database, branch_id: int | None = None) -> list[dict]:
    """Held orders, optionally only one branch's."""
    out = []
    if branch_id is None:
        rows = db.all("SELECT * FROM held_carts ORDER BY id")
    else:
        rows = db.all(f"SELECT * FROM held_carts WHERE COALESCE(branch_id, {HEAD}) = ? ORDER BY id",
                      (branch_id,))
    for r in rows:
        cart = Cart.from_json(r["data"])
        out.append({"id": r["id"], "created_at": r["created_at"], "label": r["label"],
                    "items": cart.item_count, "cart": cart})
    return out


def take_held(db: Database, held_id: int, branch_id: int | None = None) -> Cart:
    if branch_id is None:
        row = db.one("SELECT data FROM held_carts WHERE id = ?", (held_id,))
    else:
        row = db.one(f"SELECT data FROM held_carts WHERE id = ? AND COALESCE(branch_id, {HEAD}) = ?",
                     (held_id, branch_id))
    if row is None:
        raise SaleError("Held order not found")
    db.execute("DELETE FROM held_carts WHERE id = ?", (held_id,))
    return Cart.from_json(row["data"])
