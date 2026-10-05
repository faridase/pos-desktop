import sqlite3
from datetime import datetime, timedelta

import pytest

from pos_desktop.core import auth, catalog, reports, sales, shifts
from pos_desktop.core.auth import AuthError
from pos_desktop.core.catalog import Product
from pos_desktop.core.db import MIGRATIONS, SCHEMA_VERSION, Database


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def staff(db):
    owner = auth.create_user(db, "owner", "Olivia Owner", "admin", "secret99")
    manager = auth.create_user(db, "maya", "Maya Manager", "manager", "manager1")
    cashier = auth.create_user(db, "carl", "Carl Cashier", "cashier", "1234")
    return owner, manager, cashier


def test_password_hashing():
    h = auth.hash_password("hunter22")
    assert h.startswith("pbkdf2_sha256$") and "hunter22" not in h
    assert auth.verify_password("hunter22", h)
    assert not auth.verify_password("hunter23", h)
    assert not auth.verify_password("x", "garbage")
    assert auth.hash_password("same") != auth.hash_password("same")  # salted


def test_role_permissions():
    assert auth.PERMISSIONS["cashier"] < auth.PERMISSIONS["manager"] < auth.PERMISSIONS["admin"]
    cashier = auth.User(1, "c", "C", "cashier", True)
    assert cashier.can("register") and not cashier.can("refund")
    assert not auth.User(2, "m", "M", "manager", True).can("settings")


def test_create_user_validation(db, staff):
    with pytest.raises(AuthError):
        auth.create_user(db, "OWNER", "Dup", "cashier", "1234")  # case-insensitive
    with pytest.raises(AuthError):
        auth.create_user(db, "bad name", "X", "cashier", "1234")
    with pytest.raises(AuthError):
        auth.create_user(db, "boss2", "X", "admin", "12345")  # admins need 6+
    auth.create_user(db, "till2", "Till", "cashier", "4321")


def test_login_success_and_failure(db, staff):
    user = auth.authenticate(db, "Carl", "1234")
    assert user.username == "carl" and user.role == "cashier"
    assert auth.get_user(db, user.id).last_login_at
    with pytest.raises(AuthError, match="Wrong username or password"):
        auth.authenticate(db, "carl", "9999")
    with pytest.raises(AuthError, match="Wrong username or password"):
        auth.authenticate(db, "nobody", "9999")
    actions = [e["action"] for e in auth.audit_entries(db, "2000-01-01", "2999-12-31")]
    assert actions.count("login_failed") == 2 and "login" in actions


def test_lockout_after_repeated_failures(db, staff):
    now = datetime(2026, 1, 1, 12, 0)
    for _ in range(auth.MAX_ATTEMPTS - 1):
        with pytest.raises(AuthError, match="Wrong"):
            auth.authenticate(db, "carl", "0000", now=now)
    with pytest.raises(AuthError, match="locked"):
        auth.authenticate(db, "carl", "0000", now=now)
    # Even the right password is refused while locked.
    with pytest.raises(AuthError, match="Try again in"):
        auth.authenticate(db, "carl", "1234", now=now + timedelta(minutes=1))
    later = now + timedelta(minutes=auth.LOCK_MINUTES, seconds=1)
    assert auth.authenticate(db, "carl", "1234", now=later).username == "carl"


def test_admin_reset_clears_lockout(db, staff):
    now = datetime.now()
    for _ in range(auth.MAX_ATTEMPTS):
        with pytest.raises(AuthError):
            auth.authenticate(db, "carl", "0000", now=now)
    auth.set_password(db, staff[2], "5678")
    assert auth.authenticate(db, "carl", "5678").username == "carl"


def test_inactive_user_cannot_login(db, staff):
    auth.update_user(db, staff[2], "Carl Cashier", "cashier", False)
    with pytest.raises(AuthError, match="deactivated"):
        auth.authenticate(db, "carl", "1234")


def test_last_admin_is_protected(db, staff):
    owner = staff[0]
    with pytest.raises(AuthError, match="at least one active"):
        auth.update_user(db, owner, "Olivia", "manager", True)
    with pytest.raises(AuthError, match="at least one active"):
        auth.update_user(db, owner, "Olivia", "admin", False)
    auth.create_user(db, "owner2", "Second Owner", "admin", "secret88")
    auth.update_user(db, owner, "Olivia", "manager", True)


def test_cannot_deactivate_self(db, staff):
    auth.create_user(db, "owner2", "Second Owner", "admin", "secret88")
    db.user_id = staff[0]
    with pytest.raises(AuthError, match="your own"):
        auth.update_user(db, staff[0], "Olivia", "admin", False)


def test_change_own_password(db, staff):
    with pytest.raises(AuthError, match="incorrect"):
        auth.change_own_password(db, staff[2], "0000", "9876")
    auth.change_own_password(db, staff[2], "1234", "9876")
    assert auth.authenticate(db, "carl", "9876")


def test_manager_override(db, staff):
    db.user_id = staff[2]  # cashier's session
    approver = auth.authorize(db, "maya", "manager1", "refund", "Refund on #1")
    assert approver.id == staff[1]
    with pytest.raises(AuthError, match="isn't allowed"):
        auth.authorize(db, "carl", "1234", "refund")
    # An override doesn't count as the manager logging in.
    assert auth.get_user(db, staff[1]).last_login_at is None
    log = auth.audit_entries(db, "2000-01-01", "2999-12-31")
    assert any(e["action"] == "override" and "Refund on #1" in e["details"] for e in log)


def test_records_are_stamped_with_staff(db, staff):
    owner, manager, cashier = staff
    db.user_id = owner
    pid = catalog.create_product(db, Product(sku="A", name="Tea", price=500, cost=100), 10)
    db.user_id = cashier
    shifts.open_shift(db, 0)
    cart = sales.Cart()
    cart.add(catalog.get_product(db, pid), 2)
    cart.lines[0].discount_bps = 1000
    sale_id = sales.checkout(db, cart, {"cash": 990}, approved_by=manager)
    sale = sales.get_sale(db, sale_id)
    assert sale["cashier"] == "Carl Cashier" and sale["approver"] == "Maya Manager"
    item = sale["items"][0]["id"]
    sales.refund(db, sale_id, {item: 1}, "cash", approved_by=manager)
    refund = sales.get_sale(db, sale_id)["refunds"][0]
    assert refund["user_name"] == "Carl Cashier" and refund["approver"] == "Maya Manager"
    assert db.scalar("SELECT user_id FROM journal_entries ORDER BY id DESC LIMIT 1") == cashier
    assert shifts.list_shifts(db)[0]["opened_by_name"] == "Carl Cashier"
    by_staff = reports.sales_by_staff(db, "2000-01-01", "2999-12-31")
    assert by_staff[0]["name"] == "Carl Cashier" and by_staff[0]["approvals"] == 1
    assert by_staff[0]["refund_count"] == 1


def test_upgrades_v1_data_file(tmp_path):
    """A data file from the version before staff logins opens, upgrades and
    keeps its history."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute(
        "INSERT INTO sales (receipt_no, created_at, date, subtotal, discount, tax, total, "
        "tendered, change_given) VALUES (1, '2025-01-01 10:00:00', '2025-01-01', 100, 0, 10, "
        "110, 110, 0)")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.version == SCHEMA_VERSION
    assert not auth.has_users(db)
    sale = sales.get_sale(db, 1)
    assert sale["total"] == 110 and sale["cashier"] is None
    assert reports.sales_by_staff(db, "2025-01-01", "2025-01-01")[0]["name"] == \
        "(before staff logins)"
    db.close()
