"""Customers (accounts receivable) and suppliers (accounts payable)."""

from __future__ import annotations

from dataclasses import dataclass

from . import accounting as acc
from .accounting import Line
from .db import Database, now_text, today_text

PAYMENT_ACCOUNT_BY_METHOD = {"cash": "cash", "card": "card", "bank": "bank"}


class PartyError(ValueError):
    pass


@dataclass
class Party:
    id: int | None = None
    name: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    notes: str = ""
    is_active: bool = True


def _party(row) -> Party:
    return Party(row["id"], row["name"], row["phone"], row["email"], row["address"],
                 row["notes"], bool(row["is_active"]))


def _table(kind: str) -> str:
    if kind not in ("customers", "suppliers"):
        raise ValueError(kind)
    return kind


def list_parties(db: Database, kind: str, search: str = "",
                 include_inactive: bool = False) -> list[Party]:
    sql = f"SELECT * FROM {_table(kind)} WHERE 1=1"
    params: list = []
    if not include_inactive:
        sql += " AND is_active = 1"
    if search:
        sql += " AND (name LIKE ? OR phone LIKE ? OR email LIKE ?)"
        params += [f"%{search}%"] * 3
    return [_party(r) for r in db.all(sql + " ORDER BY name COLLATE NOCASE", params)]


def get_party(db: Database, kind: str, party_id: int) -> Party:
    row = db.one(f"SELECT * FROM {_table(kind)} WHERE id = ?", (party_id,))
    if row is None:
        raise PartyError("Not found")
    return _party(row)


def save_party(db: Database, kind: str, p: Party) -> int:
    if not p.name.strip():
        raise PartyError("Name is required")
    values = (p.name.strip(), p.phone.strip(), p.email.strip(), p.address.strip(),
              p.notes.strip(), int(p.is_active))
    if p.id is None:
        return db.insert(
            f"INSERT INTO {_table(kind)} (name, phone, email, address, notes, is_active, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", values + (now_text(),))
    db.execute(
        f"UPDATE {_table(kind)} SET name = ?, phone = ?, email = ?, address = ?, notes = ?, "
        "is_active = ? WHERE id = ?", values + (p.id,))
    return p.id


# ---- customers ------------------------------------------------------------------


def customer_balance(db: Database, customer_id: int) -> int:
    """What the customer owes the store."""
    charged = db.scalar(
        "SELECT SUM(p.amount) FROM sale_payments p JOIN sales s ON s.id = p.sale_id "
        "WHERE p.method = 'account' AND s.customer_id = ?", (customer_id,), 0)
    refunded = db.scalar(
        "SELECT SUM(r.net + r.tax) FROM refunds r JOIN sales s ON s.id = r.sale_id "
        "WHERE r.method = 'account' AND s.customer_id = ?", (customer_id,), 0)
    paid = db.scalar(
        "SELECT SUM(amount) FROM customer_payments WHERE customer_id = ?", (customer_id,), 0)
    return charged - refunded - paid


def receive_customer_payment(db: Database, customer_id: int, amount: int, method: str,
                             memo: str = "", date: str | None = None) -> int:
    if amount <= 0:
        raise PartyError("Amount must be positive")
    if method not in PAYMENT_ACCOUNT_BY_METHOD:
        raise PartyError("Invalid payment method")
    customer = get_party(db, "customers", customer_id)
    date = date or today_text()
    with db.transaction():
        pid = db.insert(
            "INSERT INTO customer_payments (customer_id, date, created_at, amount, method, memo) "
            "VALUES (?, ?, ?, ?, ?, ?)", (customer_id, date, now_text(), amount, method, memo))
        entry = acc.post(db, f"Payment from {customer.name}" + (f" – {memo}" if memo else ""), [
            Line(acc.account_id(db, PAYMENT_ACCOUNT_BY_METHOD[method]), debit=amount),
            Line(acc.account_id(db, "ar"), credit=amount),
        ], source="customer_payment", source_id=pid, date=date)
        db.execute("UPDATE customer_payments SET journal_entry_id = ? WHERE id = ?", (entry, pid))
    return pid


def customer_statement(db: Database, customer_id: int) -> list[dict]:
    """Chronological account activity with a running balance."""
    rows = [dict(r) for r in db.all(
        "SELECT s.date, 'Sale #' || s.receipt_no AS description, p.amount AS charge, 0 AS payment "
        "FROM sale_payments p JOIN sales s ON s.id = p.sale_id "
        "WHERE p.method = 'account' AND s.customer_id = :c "
        "UNION ALL "
        "SELECT r.date, 'Refund on #' || s.receipt_no, 0, r.net + r.tax "
        "FROM refunds r JOIN sales s ON s.id = r.sale_id "
        "WHERE r.method = 'account' AND s.customer_id = :c "
        "UNION ALL "
        "SELECT date, 'Payment (' || method || ')' || CASE WHEN memo <> '' THEN ' – ' || memo ELSE '' END, "
        "0, amount FROM customer_payments WHERE customer_id = :c "
        "ORDER BY 1", {"c": customer_id})]
    balance = 0
    for r in rows:
        balance += r["charge"] - r["payment"]
        r["balance"] = balance
    return rows


# ---- suppliers ------------------------------------------------------------------


def supplier_balance(db: Database, supplier_id: int) -> int:
    """What the store owes the supplier."""
    ap = acc.account_id(db, "ap")
    purchases = db.scalar(
        "SELECT SUM(total) FROM purchases WHERE supplier_id = ? AND pay_account_id = ?",
        (supplier_id, ap), 0)
    expenses = db.scalar(
        "SELECT SUM(amount) FROM expenses WHERE supplier_id = ? AND paid_from_account_id = ? "
        "AND voided = 0", (supplier_id, ap), 0)
    paid = db.scalar(
        "SELECT SUM(amount) FROM supplier_payments WHERE supplier_id = ?", (supplier_id,), 0)
    return purchases + expenses - paid


def supplier_statement(db: Database, supplier_id: int) -> list[dict]:
    """Purchases and expenses on credit, and payments, with running balance.
    Purchases paid immediately are listed for reference with no balance effect."""
    ap = acc.account_id(db, "ap")
    rows = [dict(r) for r in db.all(
        "SELECT date, 'Purchase #' || id || CASE WHEN reference <> '' THEN ' (' || reference || ')' "
        "ELSE '' END || CASE WHEN pay_account_id <> :ap THEN ' – paid' ELSE '' END AS description, "
        "CASE WHEN pay_account_id = :ap THEN total ELSE 0 END AS charge, 0 AS payment, total AS amount "
        "FROM purchases WHERE supplier_id = :s "
        "UNION ALL "
        "SELECT date, 'Expense' || CASE WHEN memo <> '' THEN ': ' || memo ELSE '' END, "
        "CASE WHEN paid_from_account_id = :ap THEN amount ELSE 0 END, 0, amount "
        "FROM expenses WHERE supplier_id = :s AND voided = 0 "
        "UNION ALL "
        "SELECT date, 'Payment' || CASE WHEN memo <> '' THEN ' – ' || memo ELSE '' END, 0, amount, amount "
        "FROM supplier_payments WHERE supplier_id = :s ORDER BY 1",
        {"s": supplier_id, "ap": ap})]
    balance = 0
    for r in rows:
        balance += r["charge"] - r["payment"]
        r["balance"] = balance
    return rows


def pay_supplier(db: Database, supplier_id: int, amount: int, from_account_id: int,
                 memo: str = "", date: str | None = None) -> int:
    if amount <= 0:
        raise PartyError("Amount must be positive")
    supplier = get_party(db, "suppliers", supplier_id)
    date = date or today_text()
    with db.transaction():
        pid = db.insert(
            "INSERT INTO supplier_payments (supplier_id, date, created_at, amount, account_id, memo) "
            "VALUES (?, ?, ?, ?, ?, ?)", (supplier_id, date, now_text(), amount, from_account_id, memo))
        entry = acc.post(db, f"Payment to {supplier.name}" + (f" – {memo}" if memo else ""), [
            Line(acc.account_id(db, "ap"), debit=amount),
            Line(from_account_id, credit=amount),
        ], source="supplier_payment", source_id=pid, date=date)
        db.execute("UPDATE supplier_payments SET journal_entry_id = ? WHERE id = ?", (entry, pid))
    return pid
