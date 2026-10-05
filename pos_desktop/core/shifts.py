"""Cash drawer shifts: open with a float, close with a count; any
difference is booked to Cash Over/Short."""

from __future__ import annotations

from . import accounting as acc
from .accounting import Line
from .db import Database, now_text


class ShiftError(ValueError):
    pass


HEAD = "(SELECT id FROM branches WHERE is_head = 1)"


def current_shift(db: Database, branch_id: int | None = None) -> dict | None:
    """The open shift at one branch (default: the head branch). Shifts with no
    branch count as the head branch's."""
    row = db.one(
        f"SELECT * FROM shifts WHERE closed_at IS NULL AND COALESCE(branch_id, {HEAD}) = "
        f"COALESCE(?, {HEAD}) ORDER BY id DESC LIMIT 1", (branch_id,))
    return dict(row) if row else None


def open_shift(db: Database, opening_float: int, branch_id: int | None = None) -> int:
    if opening_float < 0:
        raise ShiftError("Float can't be negative")
    if current_shift(db, branch_id):
        raise ShiftError("A shift is already open")
    return db.insert("INSERT INTO shifts (opened_at, opening_float, opened_by, branch_id) "
                     f"VALUES (?, ?, ?, COALESCE(?, {HEAD}))",
                     (now_text(), opening_float, db.user_id, branch_id))


def cash_activity(db: Database, since: str) -> int:
    """Net change in Cash on Hand from entries created since `since`."""
    cash = acc.account_id(db, "cash")
    row = db.one(
        "SELECT COALESCE(SUM(l.debit), 0) - COALESCE(SUM(l.credit), 0) FROM journal_lines l "
        "JOIN journal_entries e ON e.id = l.entry_id WHERE l.account_id = ? AND e.created_at >= ?",
        (cash, since))
    return row[0] if row else 0


def expected_cash(db: Database, shift: dict) -> int:
    return shift["opening_float"] + cash_activity(db, shift["opened_at"])


def close_shift(db: Database, counted_cash: int, notes: str = "",
                branch_id: int | None = None) -> dict:
    shift = current_shift(db, branch_id)
    if shift is None:
        raise ShiftError("No shift is open")
    if counted_cash < 0:
        raise ShiftError("Counted cash can't be negative")
    expected = expected_cash(db, shift)
    difference = counted_cash - expected
    with db.transaction():
        entry = None
        if difference:
            cash, over_short = acc.account_id(db, "cash"), acc.account_id(db, "over_short")
            lines = (
                [Line(cash, debit=difference), Line(over_short, credit=difference)]
                if difference > 0 else
                [Line(over_short, debit=-difference), Line(cash, credit=-difference)]
            )
            entry = acc.post(db, f"Cash {'over' if difference > 0 else 'short'} – shift #{shift['id']}",
                             lines, source="shift", source_id=shift["id"])
        db.execute(
            "UPDATE shifts SET closed_at = ?, expected_cash = ?, counted_cash = ?, difference = ?, "
            "notes = ?, journal_entry_id = ?, closed_by = ? WHERE id = ?",
            (now_text(), expected, counted_cash, difference, notes, entry, db.user_id, shift["id"]))
    return {**shift, "expected_cash": expected, "counted_cash": counted_cash,
            "difference": difference}


def list_shifts(db: Database, limit: int = 100) -> list[dict]:
    return [dict(r) for r in db.all(
        "SELECT s.*, o.full_name AS opened_by_name, c.full_name AS closed_by_name, b.name AS branch "
        "FROM shifts s LEFT JOIN users o ON o.id = s.opened_by LEFT JOIN users c ON c.id = s.closed_by "
        f"LEFT JOIN branches b ON b.id = COALESCE(s.branch_id, {HEAD}) "
        "ORDER BY s.id DESC LIMIT ?", (limit,))]
