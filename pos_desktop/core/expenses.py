"""Operating expenses."""

from __future__ import annotations

from . import accounting as acc
from .accounting import Line
from .db import Database, now_text, today_text


class ExpenseError(ValueError):
    pass


def record_expense(db: Database, account_id: int, paid_from_account_id: int, amount: int,
                   payee: str = "", memo: str = "", supplier_id: int | None = None,
                   date: str | None = None) -> int:
    """Records an expense paid from a money account, or on credit (Accounts
    Payable, which requires a supplier)."""
    if amount <= 0:
        raise ExpenseError("Amount must be positive")
    if acc.get_account(db, account_id).type != "expense":
        raise ExpenseError("Choose an expense account")
    if paid_from_account_id == acc.account_id(db, "ap") and supplier_id is None:
        raise ExpenseError("Choose a supplier for expenses on credit")
    if supplier_id and not payee:
        payee = db.scalar("SELECT name FROM suppliers WHERE id = ?", (supplier_id,), "")
    date = date or today_text()
    with db.transaction():
        eid = db.insert(
            "INSERT INTO expenses (date, created_at, account_id, paid_from_account_id, "
            "supplier_id, payee, amount, memo, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, now_text(), account_id, paid_from_account_id, supplier_id, payee, amount, memo,
             db.user_id))
        description = " – ".join(x for x in (payee, memo) if x) or "Expense"
        entry = acc.post(db, description, [
            Line(account_id, debit=amount),
            Line(paid_from_account_id, credit=amount),
        ], source="expense", source_id=eid, date=date)
        db.execute("UPDATE expenses SET journal_entry_id = ? WHERE id = ?", (entry, eid))
    return eid


def void_expense(db: Database, expense_id: int) -> None:
    row = db.one("SELECT * FROM expenses WHERE id = ?", (expense_id,))
    if row is None:
        raise ExpenseError("Expense not found")
    if row["voided"]:
        raise ExpenseError("Already voided")
    with db.transaction():
        acc.reverse(db, row["journal_entry_id"], memo=f"Void expense #{expense_id}")
        db.execute("UPDATE expenses SET voided = 1 WHERE id = ?", (expense_id,))


def list_expenses(db: Database, start: str, end: str, include_voided: bool = True) -> list[dict]:
    sql = (
        "SELECT e.*, a.code || ' · ' || a.name AS account, f.name AS paid_from "
        "FROM expenses e JOIN accounts a ON a.id = e.account_id "
        "JOIN accounts f ON f.id = e.paid_from_account_id "
        "WHERE e.date >= ? AND e.date <= ?"
    )
    if not include_voided:
        sql += " AND e.voided = 0"
    return [dict(r) for r in db.all(sql + " ORDER BY e.date DESC, e.id DESC", (start, end))]
