"""Double-entry general ledger and financial statements.

Every business event (sale, refund, purchase, expense, payment, stock
adjustment) posts a balanced journal entry here. Reports are derived only
from journal lines, so the books always reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .db import Database, now_text, today_text

ACCOUNT_TYPES = ("asset", "liability", "equity", "income", "expense")
TYPE_LABELS = {
    "asset": "Asset",
    "liability": "Liability",
    "equity": "Equity",
    "income": "Income",
    "expense": "Expense",
}
DEBIT_NORMAL = {"asset", "expense"}

# Accounts money can be received into or paid out of.
MONEY_ACCOUNT_KEYS = ("cash", "bank", "card")


class AccountingError(ValueError):
    pass


@dataclass
class Account:
    id: int
    code: str
    name: str
    type: str
    system_key: str | None
    is_active: bool

    @property
    def label(self) -> str:
        return f"{self.code} · {self.name}"


@dataclass
class Line:
    account_id: int
    debit: int = 0
    credit: int = 0
    memo: str = ""


@dataclass
class StatementRow:
    account_id: int | None
    code: str
    name: str
    amount: int


@dataclass
class Section:
    title: str
    rows: list[StatementRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(r.amount for r in self.rows)


def _account(row) -> Account:
    return Account(
        row["id"], row["code"], row["name"], row["type"],
        row["system_key"], bool(row["is_active"]),
    )


# ---- chart of accounts -------------------------------------------------------


def list_accounts(db: Database, include_inactive: bool = False,
                  types: tuple[str, ...] | None = None) -> list[Account]:
    sql = "SELECT * FROM accounts WHERE 1=1"
    params: list = []
    if not include_inactive:
        sql += " AND is_active = 1"
    if types:
        sql += f" AND type IN ({','.join('?' * len(types))})"
        params += list(types)
    return [_account(r) for r in db.all(sql + " ORDER BY code", params)]


def get_account(db: Database, account_id: int) -> Account:
    row = db.one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if row is None:
        raise AccountingError(f"Account {account_id} not found")
    return _account(row)


def account_id(db: Database, system_key: str) -> int:
    value = db.scalar("SELECT id FROM accounts WHERE system_key = ?", (system_key,))
    if value is None:
        raise AccountingError(f"System account '{system_key}' is missing")
    return int(value)


def money_accounts(db: Database) -> list[Account]:
    """Accounts that hold money: cash, bank, card clearing and any other
    active asset account the user added (e.g. a second bank)."""
    excluded = ("ar", "inventory")
    return [
        a for a in list_accounts(db, types=("asset",))
        if a.system_key not in excluded
    ]


def save_account(db: Database, code: str, name: str, type_: str,
                 account_id_: int | None = None, is_active: bool = True) -> int:
    code, name = code.strip(), name.strip()
    if not code or not name:
        raise AccountingError("Code and name are required")
    if type_ not in ACCOUNT_TYPES:
        raise AccountingError("Invalid account type")
    clash = db.scalar(
        "SELECT id FROM accounts WHERE code = ? AND id IS NOT ?", (code, account_id_)
    )
    if clash is not None:
        raise AccountingError(f"Account code {code} is already used")
    with db.transaction():
        if account_id_ is None:
            return db.insert(
                "INSERT INTO accounts (code, name, type, is_active) VALUES (?, ?, ?, ?)",
                (code, name, type_, int(is_active)),
            )
        existing = get_account(db, account_id_)
        if existing.system_key:
            if type_ != existing.type:
                raise AccountingError("The type of a system account can't change")
            if not is_active:
                raise AccountingError("System accounts can't be deactivated")
        elif existing.type != type_ and _has_lines(db, account_id_):
            raise AccountingError("Can't change the type of an account with postings")
        db.execute(
            "UPDATE accounts SET code = ?, name = ?, type = ?, is_active = ? WHERE id = ?",
            (code, name, type_, int(is_active), account_id_),
        )
        return account_id_


def _has_lines(db: Database, account_id_: int) -> bool:
    return db.scalar(
        "SELECT 1 FROM journal_lines WHERE account_id = ? LIMIT 1", (account_id_,)
    ) is not None


# ---- posting -----------------------------------------------------------------


def post(db: Database, memo: str, lines: list[Line], source: str = "manual",
         source_id: int | None = None, date: str | None = None) -> int:
    """Posts a balanced journal entry and returns its id."""
    lines = [l for l in lines if l.debit or l.credit]
    if len(lines) < 2:
        raise AccountingError("An entry needs at least two lines")
    for l in lines:
        if l.debit < 0 or l.credit < 0:
            raise AccountingError("Amounts can't be negative")
        if l.debit and l.credit:
            raise AccountingError("A line can't have both a debit and a credit")
    debits = sum(l.debit for l in lines)
    credits = sum(l.credit for l in lines)
    if debits != credits:
        raise AccountingError(f"Entry is out of balance ({debits} ≠ {credits})")
    with db.transaction():
        entry_id = db.insert(
            "INSERT INTO journal_entries (date, memo, source, source_id, created_at, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date or today_text(), memo, source, source_id, now_text(), db.user_id),
        )
        for l in lines:
            db.execute(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit, memo) "
                "VALUES (?, ?, ?, ?, ?)",
                (entry_id, l.account_id, l.debit, l.credit, l.memo),
            )
    return entry_id


def reverse(db: Database, entry_id: int, memo: str | None = None,
            date: str | None = None) -> int:
    """Posts the mirror image of an entry and links the two."""
    entry = db.one("SELECT * FROM journal_entries WHERE id = ?", (entry_id,))
    if entry is None:
        raise AccountingError("Entry not found")
    if entry["reversed_by"]:
        raise AccountingError("Entry was already reversed")
    lines = db.all("SELECT * FROM journal_lines WHERE entry_id = ?", (entry_id,))
    with db.transaction():
        new_id = post(
            db,
            memo or f"Reversal of #{entry_id}: {entry['memo']}",
            [Line(l["account_id"], l["credit"], l["debit"], l["memo"]) for l in lines],
            source="reversal",
            source_id=entry_id,
            date=date,
        )
        db.execute(
            "UPDATE journal_entries SET reversed_by = ? WHERE id = ?", (new_id, entry_id)
        )
    return new_id


# ---- queries -----------------------------------------------------------------


def _totals(db: Database, start: str | None = None, end: str | None = None
            ) -> dict[int, tuple[int, int]]:
    sql = (
        "SELECT l.account_id, SUM(l.debit) AS d, SUM(l.credit) AS c "
        "FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id WHERE 1=1"
    )
    params: list = []
    if start:
        sql += " AND e.date >= ?"
        params.append(start)
    if end:
        sql += " AND e.date <= ?"
        params.append(end)
    rows = db.all(sql + " GROUP BY l.account_id", params)
    return {r["account_id"]: (r["d"] or 0, r["c"] or 0) for r in rows}


def natural_balance(type_: str, debit: int, credit: int) -> int:
    """Positive when the account has its normal balance."""
    return debit - credit if type_ in DEBIT_NORMAL else credit - debit


def balances(db: Database, as_of: str | None = None,
             start: str | None = None) -> dict[int, int]:
    totals = _totals(db, start, as_of)
    return {
        a.id: natural_balance(a.type, *totals.get(a.id, (0, 0)))
        for a in list_accounts(db, include_inactive=True)
    }


def account_balance(db: Database, account_id_: int, as_of: str | None = None) -> int:
    return balances(db, as_of).get(account_id_, 0)


def trial_balance(db: Database, as_of: str | None = None) -> list[dict]:
    totals = _totals(db, None, as_of)
    rows = []
    for a in list_accounts(db, include_inactive=True):
        d, c = totals.get(a.id, (0, 0))
        net = d - c
        if net == 0 and not (d or c):
            continue
        rows.append({
            "account": a,
            "debit": net if net > 0 else 0,
            "credit": -net if net < 0 else 0,
        })
    return rows


def profit_and_loss(db: Database, start: str, end: str) -> dict:
    period = balances(db, as_of=end, start=start)
    accounts = list_accounts(db, include_inactive=True)
    cogs_keys = ("cogs", "shrinkage")

    def section(title: str, predicate) -> Section:
        return Section(title, [
            StatementRow(a.id, a.code, a.name, period[a.id])
            for a in accounts if predicate(a) and period.get(a.id)
        ])

    income = section("Revenue", lambda a: a.type == "income")
    cogs = section("Cost of sales", lambda a: a.type == "expense" and a.system_key in cogs_keys)
    expenses = section(
        "Operating expenses",
        lambda a: a.type == "expense" and a.system_key not in cogs_keys,
    )
    gross = income.total - cogs.total
    return {
        "income": income,
        "cogs": cogs,
        "expenses": expenses,
        "gross_profit": gross,
        "net_income": gross - expenses.total,
    }


def net_income(db: Database, as_of: str | None = None, start: str | None = None) -> int:
    bal = balances(db, as_of, start)
    total = 0
    for a in list_accounts(db, include_inactive=True):
        if a.type == "income":
            total += bal[a.id]
        elif a.type == "expense":
            total -= bal[a.id]
    return total


def balance_sheet(db: Database, as_of: str) -> dict:
    bal = balances(db, as_of)
    accounts = list_accounts(db, include_inactive=True)

    def section(title: str, type_: str) -> Section:
        return Section(title, [
            StatementRow(a.id, a.code, a.name, bal[a.id])
            for a in accounts if a.type == type_ and bal.get(a.id)
        ])

    assets = section("Assets", "asset")
    liabilities = section("Liabilities", "liability")
    equity = section("Equity", "equity")
    earnings = net_income(db, as_of)
    if earnings:
        equity.rows.append(StatementRow(None, "", "Retained earnings (to date)", earnings))
    return {
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "balanced": assets.total == liabilities.total + equity.total,
    }


def general_ledger(db: Database, account_id_: int, start: str, end: str) -> dict:
    account = get_account(db, account_id_)
    before = _totals(db, None, _day_before(start)).get(account_id_, (0, 0))
    opening = natural_balance(account.type, *before)
    rows = db.all(
        "SELECT e.id AS entry_id, e.date, e.memo AS entry_memo, e.source, "
        "l.debit, l.credit, l.memo FROM journal_lines l "
        "JOIN journal_entries e ON e.id = l.entry_id "
        "WHERE l.account_id = ? AND e.date >= ? AND e.date <= ? "
        "ORDER BY e.date, e.id, l.id",
        (account_id_, start, end),
    )
    running = opening
    out = []
    for r in rows:
        running += natural_balance(account.type, r["debit"], r["credit"])
        out.append({
            "entry_id": r["entry_id"],
            "date": r["date"],
            "memo": r["memo"] or r["entry_memo"],
            "source": r["source"],
            "debit": r["debit"],
            "credit": r["credit"],
            "balance": running,
        })
    return {"account": account, "opening": opening, "rows": out, "closing": running}


def journal(db: Database, start: str, end: str, source: str | None = None,
            search: str = "") -> list[dict]:
    sql = ("SELECT e.*, u.full_name AS user_name FROM journal_entries e "
           "LEFT JOIN users u ON u.id = e.user_id WHERE e.date >= ? AND e.date <= ?")
    params: list = [start, end]
    if source:
        sql += " AND e.source = ?"
        params.append(source)
    if search:
        sql += " AND e.memo LIKE ?"
        params.append(f"%{search}%")
    entries = db.all(sql + " ORDER BY e.date DESC, e.id DESC", params)
    if not entries:
        return []
    ids = [e["id"] for e in entries]
    lines = db.all(
        "SELECT l.*, a.code, a.name FROM journal_lines l "
        "JOIN accounts a ON a.id = l.account_id "
        f"WHERE l.entry_id IN ({','.join('?' * len(ids))}) ORDER BY l.id",
        ids,
    )
    by_entry: dict[int, list] = {}
    for l in lines:
        by_entry.setdefault(l["entry_id"], []).append(dict(l))
    return [
        {**dict(e), "lines": by_entry.get(e["id"], []),
         "amount": sum(l["debit"] for l in by_entry.get(e["id"], []))}
        for e in entries
    ]


def _day_before(date: str) -> str:
    from datetime import date as _date, timedelta
    return (_date.fromisoformat(date) - timedelta(days=1)).isoformat()
