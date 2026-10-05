"""Branches and customer orders. Mirrors web/server/test/branches-orders.test.ts
so both apps behave the same on the shared data file."""

import sqlite3
from datetime import datetime, timezone

import pytest

from pos_desktop.core import auth, branches, catalog, orders, sales, shifts
from pos_desktop.core.branches import Branch, BranchError
from pos_desktop.core.catalog import Product
from pos_desktop.core.db import MIGRATIONS, SCHEMA_VERSION, Database
from pos_desktop.core.orders import OrderError, OrderInput, OrderItemInput


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def make_product(db, name="Tea", price=500):
    return catalog.create_product(db, Product(sku=catalog.next_sku(db), name=name, price=price,
                                              cost=100), opening_stock=10)


def order(**kw):
    base = dict(customer_name="Ana", address="1 Main St", phone="555-0100",
                scheduled_date="2999-01-01", items=[])
    base.update(kw)
    return OrderInput(**base)


# ---- branches ------------------------------------------------------------------


def test_head_branch_and_default_stamping(db):
    head = branches.list_branches(db)[0]
    assert head.is_head and head.code == "HQ"
    pid = make_product(db)
    cart = sales.Cart()
    cart.add(catalog.get_product(db, pid))
    sale_id = sales.checkout(db, cart, {"cash": 550})
    assert db.scalar("SELECT branch_id FROM sales WHERE id = ?", (sale_id,)) == head.id


def test_branch_crud_and_protection(db):
    head = branches.head_branch(db)
    north = branches.save_branch(db, Branch(code="north", name="North Store"))
    assert branches.get_branch(db, north).code == "NORTH"
    with pytest.raises(BranchError, match="already used"):
        branches.save_branch(db, Branch(code="NORTH", name="Dup"))
    branches.save_branch(db, Branch(id=north, code="N1", name="North", is_active=False))
    assert not branches.get_branch(db, north).is_active
    with pytest.raises(BranchError, match="can't be deactivated"):
        branches.save_branch(db, Branch(id=head.id, code="HQ", name="Head", is_active=False))
    with pytest.raises(BranchError, match="head branch"):
        branches.delete_branch(db, head.id)
    south = branches.save_branch(db, Branch(code="S", name="South"))
    auth.create_user(db, "sam", "Sam", "cashier", "1234", branch_id=south)
    with pytest.raises(BranchError, match="Deactivate it instead"):
        branches.delete_branch(db, south)
    branches.delete_branch(db, north)
    assert len(branches.list_branches(db)) == 2


def test_api_keys(db):
    bid = branches.save_branch(db, Branch(code="W", name="Web"))
    key = branches.generate_api_key(db, bid)
    assert key.startswith("pos_")
    assert key not in db.scalar("SELECT api_key_hash FROM branches WHERE id = ?", (bid,))
    assert branches.branch_for_api_key(db, key).id == bid
    assert branches.branch_for_api_key(db, key + "x") is None
    replaced = branches.generate_api_key(db, bid)
    assert branches.branch_for_api_key(db, key) is None
    branches.save_branch(db, Branch(id=bid, code="W", name="Web", is_active=False))
    assert branches.branch_for_api_key(db, replaced) is None


def test_shifts_and_held_orders_are_per_branch(db):
    head = branches.head_branch(db).id
    other = branches.save_branch(db, Branch(code="B2", name="B2"))
    shifts.open_shift(db, 100, head)
    shifts.open_shift(db, 200, other)
    assert shifts.current_shift(db, other)["opening_float"] == 200
    assert shifts.current_shift(db)["opening_float"] == 100
    pid = make_product(db)
    cart = sales.Cart()
    cart.add(catalog.get_product(db, pid))
    held = sales.hold_cart(db, cart, "x", other)
    assert sales.list_held(db, head) == []
    with pytest.raises(sales.SaleError, match="not found"):
        sales.take_held(db, held, head)
    assert sales.take_held(db, held, other).lines


def test_staff_branch_and_order_permission(db):
    uid = auth.create_user(db, "ana", "Ana", "cashier", "1234", handles_orders=True)
    ana = auth.get_user(db, uid)
    assert ana.branch_id == branches.head_branch(db).id and ana.can("handle_orders")
    assert not ana.can("orders")
    other = branches.save_branch(db, Branch(code="B2", name="B2"))
    auth.update_user(db, uid, "Ana", "cashier", True, branch_id=other, handles_orders=False)
    ana = auth.get_user(db, uid)
    assert ana.branch_id == other and not ana.can("handle_orders")
    admin = auth.User(1, "o", "O", "admin", True)
    assert admin.can("orders") and admin.can("branches") and admin.can("handle_orders")
    assert "handle_orders" in auth.permissions_of("cashier", True)


def test_v2_file_upgrades_into_head_branch(tmp_path):
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN;\n{MIGRATIONS[0]}\n{MIGRATIONS[1]}\nPRAGMA user_version = 2;\nCOMMIT;")
    conn.execute("INSERT INTO sales (receipt_no, created_at, date, subtotal, discount, tax, total, "
                 "tendered, change_given) VALUES (1, '2025-01-01 10:00:00', '2025-01-01', 100, 0, 10, "
                 "110, 110, 0)")
    conn.commit()
    conn.close()
    d = Database(path)
    assert d.version == SCHEMA_VERSION
    assert d.scalar("SELECT branch_id FROM sales") == branches.head_branch(d).id
    d.close()


# ---- orders ----------------------------------------------------------------------


@pytest.fixture
def setup(db):
    head = branches.head_branch(db).id
    pid = make_product(db)
    sku = catalog.get_product(db, pid).sku
    ana = auth.create_user(db, "ana", "Ana", "cashier", "1234", handles_orders=True)
    ben = auth.create_user(db, "ben", "Ben", "cashier", "1234", handles_orders=True)
    return db, head, pid, sku, ana, ben


def as_user(db, uid, fn):
    db.user_id = uid
    try:
        return fn()
    finally:
        db.user_id = None


def test_receive_and_duplicates(setup):
    db, head, pid, sku, _, _ = setup
    oid, dup = orders.receive_order(db, head, order(reference="WEB-1", items=[
        OrderItemInput(2, sku=sku.lower()), OrderItemInput(1, product_id=pid)]), "api")
    assert not dup
    o = orders.get_order(db, oid)
    assert (o["status"], o["item_count"], o["total"], o["fulfillment"]) == ("new", 3, 1500, "delivery")
    assert orders.receive_order(db, head, order(reference="WEB-1", items=[OrderItemInput(9, sku=sku)])) \
        == (oid, True)
    p = catalog.get_product(db, pid)
    p.price = 999
    catalog.update_product(db, p)
    assert orders.get_order(db, oid)["items"][0]["unit_price"] == 500


def test_validation(setup):
    db, head, _, sku, _, _ = setup
    item = [OrderItemInput(1, sku=sku)]
    with pytest.raises(OrderError, match="at least one item"):
        orders.receive_order(db, head, order())
    with pytest.raises(OrderError, match="Unknown products: SKU NOPE"):
        orders.receive_order(db, head, order(items=[OrderItemInput(1, sku="NOPE")]))
    with pytest.raises(OrderError, match="address"):
        orders.receive_order(db, head, order(address=" ", items=item))
    with pytest.raises(OrderError, match="date"):
        orders.receive_order(db, head, order(scheduled_date="2026-02-30", items=item))
    with pytest.raises(OrderError, match="before the order date"):
        orders.receive_order(db, head, order(ordered_at="2030-05-02 10:00:00",
                                             scheduled_date="2030-05-01", items=item))
    utc = datetime(2026, 10, 1, 2, 30, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert orders.parse_timestamp("2026-10-01T02:30:00Z") == utc
    assert orders.parse_timestamp("2026-10-01 09:30") == "2026-10-01 09:30:00"
    assert orders.parse_timestamp("yesterday") is None


def test_pickup_orders_and_filters(setup):
    db, head, _, sku, _, _ = setup
    item = [OrderItemInput(1, sku=sku)]
    pickup, _ = orders.receive_order(db, head, order(fulfillment="pickup", address="", items=item))
    assert orders.get_order(db, pickup)["fulfillment"] == "pickup"
    with pytest.raises(OrderError, match="fulfillment must be"):
        orders.receive_order(db, head, order(fulfillment="drone", items=item))
    orders.receive_order(db, head, order(items=item))
    assert [o["id"] for o in orders.list_orders(db, None, fulfillment="pickup")] == [pickup]
    orders.update_order(db, pickup, order(address="", items=item))  # keeps pickup
    assert orders.get_order(db, pickup)["fulfillment"] == "pickup"
    with pytest.raises(OrderError, match="address"):
        orders.update_order(db, pickup, order(fulfillment="delivery", address="", items=item))


def test_workflow(setup):
    db, head, _, sku, ana, ben = setup
    oid, _ = orders.receive_order(db, head, order(items=[OrderItemInput(1, sku=sku)]))
    with pytest.raises(OrderError, match="Take order"):
        as_user(db, ana, lambda: orders.complete(db, oid))
    with pytest.raises(OrderError, match="Take the order first"):
        as_user(db, ana, lambda: orders.cancel(db, oid, "x"))
    as_user(db, ana, lambda: orders.pick_up(db, oid))
    with pytest.raises(OrderError, match="already taken by Ana"):
        as_user(db, ben, lambda: orders.pick_up(db, oid))
    with pytest.raises(OrderError, match="handled by Ana"):
        as_user(db, ben, lambda: orders.complete(db, oid))
    as_user(db, ana, lambda: orders.release(db, oid))
    as_user(db, ben, lambda: orders.pick_up(db, oid))
    as_user(db, ben, lambda: orders.complete(db, oid, "At the door"))
    done = orders.get_order(db, oid)
    assert done["status"] == "completed" and done["closed_by_name"] == "Ben"
    second, _ = orders.receive_order(db, head, order(items=[OrderItemInput(1, sku=sku)]))
    orders.cancel(db, second, "Customer called", manage=True)
    assert orders.list_orders(db, head) == []
    assert len(orders.list_orders(db, head, status="all")) == 2
    actions = [e["action"] for e in auth.audit_entries(db, "2000-01-01", "2999-12-31")]
    assert {"order_picked_up", "order_completed", "order_cancelled"} <= set(actions)
    orders.delete_order(db, oid)
    assert db.scalar("SELECT COUNT(*) FROM order_items WHERE order_id = ?", (oid,)) == 0
