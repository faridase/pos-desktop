"""Branches (stores). Every data file has exactly one head branch, created by
migration v3. Sales, shifts, held orders, staff and customer orders belong to
a branch; a NULL branch_id means the head branch.

Each branch can have an API key that an online shop uses to send orders to
that branch through the web app's order API (the desktop app can't receive
orders from the internet, but it shares the data file, so orders the web
server receives appear here). Only a SHA-256 hash of the key is stored; the
key itself is shown once, when generated.

Same behavior as web/server/src/core/branches.ts.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

from . import auth
from .db import Database, now_text

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
USED_BY = ("users", "sales", "shifts", "held_carts", "orders")
HEAD_SQL = "(SELECT id FROM branches WHERE is_head = 1)"


class BranchError(ValueError):
    pass


@dataclass
class Branch:
    id: int | None = None
    code: str = ""
    name: str = ""
    address: str = ""
    phone: str = ""
    is_head: bool = False
    is_active: bool = True
    has_api_key: bool = False
    api_key_hint: str = ""
    created_at: str = ""


def _branch(row) -> Branch:
    return Branch(row["id"], row["code"], row["name"], row["address"], row["phone"],
                  bool(row["is_head"]), bool(row["is_active"]), row["api_key_hash"] is not None,
                  row["api_key_hint"], row["created_at"])


def list_branches(db: Database, include_inactive: bool = True) -> list[Branch]:
    sql = "SELECT * FROM branches" + ("" if include_inactive else " WHERE is_active = 1")
    return [_branch(r) for r in db.all(sql + " ORDER BY is_head DESC, name COLLATE NOCASE")]


def get_branch(db: Database, branch_id: int) -> Branch:
    row = db.one("SELECT * FROM branches WHERE id = ?", (branch_id,))
    if row is None:
        raise BranchError("Branch not found")
    return _branch(row)


def head_branch(db: Database) -> Branch:
    row = db.one("SELECT * FROM branches WHERE is_head = 1 ORDER BY id LIMIT 1")
    if row is None:
        raise BranchError("The head branch is missing")
    return _branch(row)


def current_branch(db: Database) -> Branch:
    """The branch this POS runs as. For now every till runs as the head branch."""
    return head_branch(db)


def branch_of_user(db: Database, user) -> int:
    """A staff member's branch (unset means the head branch)."""
    return user.branch_id if user.branch_id is not None else head_branch(db).id


def save_branch(db: Database, b: Branch) -> int:
    code, name = b.code.strip().upper(), b.name.strip()
    if not CODE_RE.match(code):
        raise BranchError("Code must be 1–16 letters, digits, dashes or underscores")
    if not name:
        raise BranchError("Name is required")
    if db.scalar("SELECT id FROM branches WHERE code = ? AND id IS NOT ?", (code, b.id)) is not None:
        raise BranchError(f"Branch code {code} is already used")
    values = (code, name, b.address.strip(), b.phone.strip(), int(b.is_active))
    with db.transaction():
        if b.id is None:
            new_id = db.insert(
                "INSERT INTO branches (code, name, address, phone, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", values + (now_text(),))
            auth.log(db, "branch_created", f"{code} {name}")
            return new_id
        existing = get_branch(db, b.id)
        if existing.is_head and not b.is_active:
            raise BranchError("The head branch can't be deactivated")
        db.execute("UPDATE branches SET code = ?, name = ?, address = ?, phone = ?, is_active = ? "
                   "WHERE id = ?", values + (b.id,))
        change = ""
        if existing.is_active != b.is_active:
            change = " (activated)" if b.is_active else " (deactivated)"
        auth.log(db, "branch_updated", f"{code} {name}{change}")
        return b.id


def delete_branch(db: Database, branch_id: int) -> None:
    """Deletes a branch that has never been used. Used branches can be deactivated instead."""
    branch = get_branch(db, branch_id)
    if branch.is_head:
        raise BranchError("The head branch can't be deleted")
    if any(db.scalar(f"SELECT 1 FROM {t} WHERE branch_id = ? LIMIT 1", (branch_id,)) is not None
           for t in USED_BY):
        raise BranchError("This branch has staff, sales, shifts or orders. Deactivate it instead.")
    with db.transaction():
        db.execute("DELETE FROM branches WHERE id = ?", (branch_id,))
        auth.log(db, "branch_deleted", f"{branch.code} {branch.name}")


# ---- order API keys -------------------------------------------------------------


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_api_key(db: Database, branch_id: int) -> str:
    """Creates (or replaces) the branch's order API key and returns it. It can't be shown again."""
    branch = get_branch(db, branch_id)
    if not branch.is_active:
        raise BranchError("Activate the branch first")
    key = "pos_" + secrets.token_urlsafe(30)
    with db.transaction():
        db.execute("UPDATE branches SET api_key_hash = ?, api_key_hint = ? WHERE id = ?",
                   (_hash_key(key), key[-4:], branch_id))
        auth.log(db, "api_key_generated",
                 branch.code + (" (replaced old key)" if branch.has_api_key else ""))
    return key


def revoke_api_key(db: Database, branch_id: int) -> None:
    branch = get_branch(db, branch_id)
    with db.transaction():
        db.execute("UPDATE branches SET api_key_hash = NULL, api_key_hint = '' WHERE id = ?",
                   (branch_id,))
        auth.log(db, "api_key_revoked", branch.code)


def branch_for_api_key(db: Database, key: str) -> Branch | None:
    """The active branch an API key belongs to, or None."""
    if not key.startswith("pos_") or len(key) > 200:
        return None
    row = db.one("SELECT * FROM branches WHERE api_key_hash = ? AND is_active = 1", (_hash_key(key),))
    return _branch(row) if row else None
