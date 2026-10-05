"""Staff accounts, roles, permissions and the audit log.

Passwords are stored as salted PBKDF2-SHA256 hashes. After MAX_ATTEMPTS
wrong passwords an account is locked for LOCK_MINUTES.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from .db import Database, now_text

ROLES = {"admin": "Owner / Admin", "manager": "Manager", "cashier": "Cashier"}

PERMISSION_LABELS = {
    "register": "Sell at the register",
    "shifts": "Open and close cash shifts",
    "view_sales": "View sales history and reprint receipts",
    "dashboard": "View the dashboard",
    "discount": "Give discounts and change prices",
    "refund": "Process refunds",
    "products": "Manage products",
    "inventory": "Receive and adjust stock",
    "customers": "Manage customers and receive payments",
    "suppliers": "Manage suppliers and pay bills",
    "expenses": "Record and void expenses",
    "reports": "View reports",
    "accounting": "Accounting: journal, accounts, transfers",
    "settings": "Change settings",
    "backup": "Back up, restore and load sample data",
    "users": "Manage staff and view the audit log",
    "branches": "Manage branches and order API keys",
    "orders": "Manage customer orders (delivery and pickup) at every branch",
    "handle_orders": "View and process customer orders at their branch",
}

_CASHIER = frozenset({"register", "shifts", "view_sales"})
_MANAGER = _CASHIER | {"dashboard", "discount", "refund", "products", "inventory",
                       "customers", "suppliers", "expenses", "reports"}
PERMISSIONS: dict[str, frozenset[str]] = {
    "cashier": _CASHIER,
    "manager": frozenset(_MANAGER),
    "admin": frozenset(PERMISSION_LABELS),
}

MIN_PASSWORD_LENGTH = {"admin": 6, "manager": 6, "cashier": 4}
MAX_ATTEMPTS = 5
LOCK_MINUTES = 5
ITERATIONS = 200_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,32}$")


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class User:
    id: int
    username: str
    full_name: str
    role: str
    is_active: bool
    last_login_at: str | None = None
    # Home branch; None means the head branch.
    branch_id: int | None = None
    # Granted per person: may view and process their branch's customer orders.
    handles_orders: bool = False

    @property
    def role_label(self) -> str:
        return ROLES.get(self.role, self.role)

    def can(self, permission: str) -> bool:
        if permission == "handle_orders" and self.handles_orders:
            return True
        return permission in PERMISSIONS.get(self.role, frozenset())


def permissions_of(role: str, handles_orders: bool = False) -> list[str]:
    user = User(0, "", "", role, True, handles_orders=handles_orders)
    return [p for p in PERMISSION_LABELS if user.can(p)]


def _user(row) -> User:
    return User(row["id"], row["username"], row["full_name"], row["role"],
                bool(row["is_active"]), row["last_login_at"], row["branch_id"],
                bool(row["handles_orders"]))


def _check_branch(db: Database, branch_id: int | None) -> None:
    if branch_id is None:
        return
    row = db.one("SELECT is_active FROM branches WHERE id = ?", (branch_id,))
    if row is None:
        raise AuthError("Branch not found")
    if not row["is_active"]:
        raise AuthError("That branch is inactive")


# ---- passwords -----------------------------------------------------------------


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                                     int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


_DUMMY_HASH = hash_password("timing-equaliser", 1000)


def _check_password_rules(password: str, role: str) -> None:
    minimum = MIN_PASSWORD_LENGTH.get(role, 6)
    if len(password) < minimum:
        raise AuthError(f"Password must be at least {minimum} characters for a "
                        f"{ROLES.get(role, role).lower()}")
    if password.strip() != password:
        raise AuthError("Password can't start or end with spaces")


# ---- audit log -----------------------------------------------------------------


def log(db: Database, action: str, details: str = "", user_id: int | None = None) -> None:
    db.execute("INSERT INTO audit_log (created_at, user_id, action, details) VALUES (?, ?, ?, ?)",
               (now_text(), user_id if user_id is not None else db.user_id, action, details))


def audit_entries(db: Database, start: str, end: str, user_id: int | None = None) -> list[dict]:
    sql = ("SELECT a.*, u.username, u.full_name FROM audit_log a "
           "LEFT JOIN users u ON u.id = a.user_id "
           "WHERE substr(a.created_at, 1, 10) >= ? AND substr(a.created_at, 1, 10) <= ?")
    params: list = [start, end]
    if user_id:
        sql += " AND a.user_id = ?"
        params.append(user_id)
    return [dict(r) for r in db.all(sql + " ORDER BY a.id DESC", params)]


# ---- users ---------------------------------------------------------------------


def has_users(db: Database) -> bool:
    return db.scalar("SELECT 1 FROM users LIMIT 1") is not None


def list_users(db: Database, include_inactive: bool = True) -> list[User]:
    sql = "SELECT * FROM users" + ("" if include_inactive else " WHERE is_active = 1")
    return [_user(r) for r in db.all(sql + " ORDER BY is_active DESC, full_name COLLATE NOCASE")]


def get_user(db: Database, user_id: int) -> User:
    row = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    if row is None:
        raise AuthError("User not found")
    return _user(row)


def display_name(db: Database, user_id: int | None) -> str:
    if user_id is None:
        return ""
    return db.scalar("SELECT full_name FROM users WHERE id = ?", (user_id,), "")


def _active_admins(db: Database, excluding: int | None = None) -> int:
    return db.scalar("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1 "
                     "AND id IS NOT ?", (excluding,), 0)


def create_user(db: Database, username: str, full_name: str, role: str, password: str,
                branch_id: int | None = None, handles_orders: bool = False) -> int:
    username, full_name = username.strip(), full_name.strip()
    if not USERNAME_RE.match(username):
        raise AuthError("Username must be 2–32 letters, digits, dots, dashes or underscores")
    if not full_name:
        raise AuthError("Full name is required")
    if role not in ROLES:
        raise AuthError("Invalid role")
    if db.scalar("SELECT 1 FROM users WHERE username = ?", (username,)):
        raise AuthError(f"Username “{username}” is taken")
    _check_password_rules(password, role)
    _check_branch(db, branch_id)
    with db.transaction():
        uid = db.insert(
            "INSERT INTO users (username, full_name, role, password_hash, created_at, branch_id, "
            "handles_orders) VALUES (?, ?, ?, ?, ?, "
            "COALESCE(?, (SELECT id FROM branches WHERE is_head = 1)), ?)",
            (username, full_name, role, hash_password(password), now_text(), branch_id,
             int(handles_orders)))
        log(db, "user_created", f"{username} ({ROLES[role]})")
    return uid


def update_user(db: Database, user_id: int, full_name: str, role: str, is_active: bool,
                branch_id: int | None = None, handles_orders: bool | None = None) -> None:
    """`branch_id` and `handles_orders` are left unchanged when None."""
    user = get_user(db, user_id)
    _check_branch(db, branch_id)
    if not full_name.strip():
        raise AuthError("Full name is required")
    if role not in ROLES:
        raise AuthError("Invalid role")
    if user.role == "admin" and user.is_active and (role != "admin" or not is_active) \
            and _active_admins(db, excluding=user_id) == 0:
        raise AuthError("There must always be at least one active owner/admin")
    if user_id == db.user_id and not is_active:
        raise AuthError("You can't deactivate your own account")
    with db.transaction():
        db.execute("UPDATE users SET full_name = ?, role = ?, is_active = ? WHERE id = ?",
                   (full_name.strip(), role, int(is_active), user_id))
        changes = []
        if branch_id is not None and branch_id != user.branch_id:
            db.execute("UPDATE users SET branch_id = ? WHERE id = ?", (branch_id, user_id))
            changes.append("branch changed")
        if handles_orders is not None and handles_orders != user.handles_orders:
            db.execute("UPDATE users SET handles_orders = ? WHERE id = ?",
                       (int(handles_orders), user_id))
            changes.append("may handle orders" if handles_orders else "no longer handles orders")
        if role != user.role:
            changes.append(f"role {ROLES[user.role]} → {ROLES[role]}")
        if is_active != user.is_active:
            changes.append("activated" if is_active else "deactivated")
        if full_name.strip() != user.full_name:
            changes.append("name changed")
        if changes:
            log(db, "user_updated", f"{user.username}: {', '.join(changes)}")


def set_password(db: Database, user_id: int, password: str) -> None:
    """Admin reset. Also clears any lockout."""
    user = get_user(db, user_id)
    _check_password_rules(password, user.role)
    with db.transaction():
        db.execute("UPDATE users SET password_hash = ?, failed_attempts = 0, locked_until = NULL "
                   "WHERE id = ?", (hash_password(password), user_id))
        log(db, "password_reset", user.username)


def change_own_password(db: Database, user_id: int, current: str, new: str) -> None:
    row = db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    if row is None or not verify_password(current, row["password_hash"]):
        raise AuthError("Current password is incorrect")
    _check_password_rules(new, row["role"])
    with db.transaction():
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), user_id))
        log(db, "password_changed", row["username"], user_id)


# ---- signing in ------------------------------------------------------------------


def authenticate(db: Database, username: str, password: str, now: datetime | None = None,
                 purpose: str = "login") -> User:
    """Checks credentials. `purpose` is "login" or "override" (a manager
    approving an action on someone else's session)."""
    now = now or datetime.now()
    row = db.one("SELECT * FROM users WHERE username = ?", (username.strip(),))
    if row is None:
        verify_password(password, _DUMMY_HASH)  # don't reveal which usernames exist
        with db.transaction():
            log(db, f"{purpose}_failed", f"unknown user “{username.strip()}”")
        raise AuthError("Wrong username or password")
    if not row["is_active"]:
        with db.transaction():
            log(db, f"{purpose}_failed", f"{row['username']}: account inactive")
        raise AuthError("This account is deactivated")
    if row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now:
        wait = datetime.fromisoformat(row["locked_until"]) - now
        minutes = max(1, -(-int(wait.total_seconds()) // 60))
        raise AuthError(f"Too many wrong attempts. Try again in {minutes} minute"
                        f"{'s' if minutes != 1 else ''}, or ask an admin to reset the password.")

    if not verify_password(password, row["password_hash"]):
        attempts = row["failed_attempts"] + 1
        locked = None
        if attempts >= MAX_ATTEMPTS:
            locked = (now + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
            attempts = 0
        # Commit the counter before raising, or the rollback would undo it.
        with db.transaction():
            db.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                       (attempts, locked, row["id"]))
            log(db, f"{purpose}_failed", row["username"] + (" – account locked" if locked else ""),
                row["id"])
        raise AuthError("Account locked for too many wrong attempts" if locked
                        else "Wrong username or password")
    with db.transaction():
        db.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL"
                   + (", last_login_at = ?" if purpose == "login" else "") + " WHERE id = ?",
                   ((now_text(), row["id"]) if purpose == "login" else (row["id"],)))
        if purpose == "login":
            log(db, "login", row["username"], row["id"])
    return get_user(db, row["id"])


def authorize(db: Database, username: str, password: str, permission: str,
              action: str = "") -> User:
    """A supervisor approves `permission` for the current session."""
    approver = authenticate(db, username, password, purpose="override")
    if not approver.can(permission):
        with db.transaction():
            log(db, "override_denied", f"{approver.username} lacks “{permission}”", approver.id)
        raise AuthError(f"{approver.full_name} isn't allowed to approve this")
    with db.transaction():
        log(db, "override", f"{approver.username} approved: {action or PERMISSION_LABELS[permission]}")
    return approver
