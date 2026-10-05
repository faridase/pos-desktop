import sqlite3

import pytest

from pos_desktop.core import accounting as acc
from pos_desktop.core import catalog, expenses, parties, reports, sales, shifts
from pos_desktop.core import settings as st
from pos_desktop.core.accounting import AccountingError, Line
from pos_desktop.core.catalog import Product, PurchaseLine
from pos_desktop.core.db import SCHEMA_VERSION, Database, NewerDatabaseError
from pos_desktop.core.money import MoneyFormat, allocate, format_bps, parse_bps, percent_of
from pos_desktop.core.parties import Party
from pos_desktop.core.receipts import receipt_html
from pos_desktop.core.sample_data import load_sample_data


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def bal(db, key):
    return acc.account_balance(db, acc.account_id(db, key))


def assert_books_balance(db):
    tb = acc.trial_balance(db)
    assert sum(r["debit"] for r in tb) == sum(r["credit"] for r in tb)
    assert acc.balance_sheet(db, "2999-12-31")["balanced"]


def make_product(db, name="Widget", price=1000, cost=400, stock=10, taxable=True, track=True):
    return catalog.create_product(db, Product(
        sku=catalog.next_sku(db), name=name, price=price, cost=cost,
        taxable=taxable, track_stock=track), opening_stock=stock)


# ---- money ---------------------------------------------------------------------


def test_money_format_and_parse():
    usd = MoneyFormat()
    assert usd.format(123456) == "$1,234.56"
    assert usd.format(-5) == "-$0.05"
    assert usd.parse("$1,234.5") == 123450
    assert usd.parse("abc") is None
    assert usd.parse("-1") is None
    idr = MoneyFormat("Rp", 0, ".", ",")
    assert idr.format(1500000) == "Rp1.500.000"
    assert idr.parse("1.500.000") == 1500000


def test_allocate_and_percent():
    assert allocate(100, [1, 1, 1]) == [34, 33, 33]
    assert sum(allocate(999, [5, 3, 2])) == 999
    assert percent_of(1005, 1000) == 101
    assert format_bps(725) == "7.25%" and format_bps(1000) == "10%"
    assert parse_bps("7.25") == 725 and parse_bps("101") is None


# ---- schema --------------------------------------------------------------------


def test_new_database_is_current_and_has_chart(db):
    assert db.version == SCHEMA_VERSION
    assert acc.account_id(db, "cash")


def test_refuses_database_from_newer_version(tmp_path):
    path = tmp_path / "pos.db"
    Database(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(NewerDatabaseError):
        Database(path)


def test_settings_defaults_for_missing_keys(db):
    assert st.get(db, "store_name") == "My Store"
    st.set_many(db, {"store_name": "Cafe"})
    assert st.get(db, "store_name") == "Cafe"


# ---- ledger --------------------------------------------------------------------


def test_unbalanced_entry_rejected(db):
    with pytest.raises(AccountingError):
        acc.post(db, "bad", [Line(acc.account_id(db, "cash"), debit=5),
                             Line(acc.account_id(db, "bank"), credit=4)])


def test_reverse_entry(db):
    e = acc.post(db, "x", [Line(acc.account_id(db, "cash"), debit=500),
                           Line(acc.account_id(db, "equity"), credit=500)])
    acc.reverse(db, e)
    assert bal(db, "cash") == 0
    with pytest.raises(AccountingError):
        acc.reverse(db, e)


def test_transaction_rolls_back_on_error(db):
    before = db.scalar("SELECT COUNT(*) FROM journal_entries")
    with pytest.raises(RuntimeError):
        with db.transaction():
            acc.post(db, "x", [Line(acc.account_id(db, "cash"), debit=1),
                               Line(acc.account_id(db, "equity"), credit=1)])
            raise RuntimeError
    assert db.scalar("SELECT COUNT(*) FROM journal_entries") == before


# ---- catalog -------------------------------------------------------------------


def test_opening_stock_posts_inventory(db):
    make_product(db, stock=10, cost=400)
    assert bal(db, "inventory") == 4000
    assert bal(db, "opening") == 4000


def test_purchase_weighted_average_cost_and_ap(db):
    pid = make_product(db, stock=10, cost=400)
    supplier = parties.save_party(db, "suppliers", Party(name="Acme"))
    catalog.record_purchase(db, [PurchaseLine(pid, 10, 600)], acc.account_id(db, "ap"), supplier)
    p = catalog.get_product(db, pid)
    assert p.stock == 20 and p.cost == 500
    assert parties.supplier_balance(db, supplier) == 6000
    parties.pay_supplier(db, supplier, 6000, acc.account_id(db, "bank"))
    assert parties.supplier_balance(db, supplier) == 0
    assert bal(db, "ap") == 0
    assert_books_balance(db)


def test_credit_purchase_requires_supplier(db):
    pid = make_product(db)
    with pytest.raises(catalog.CatalogError):
        catalog.record_purchase(db, [PurchaseLine(pid, 1, 1)], acc.account_id(db, "ap"))


def test_stock_adjustment_books_shrinkage(db):
    pid = make_product(db, stock=10, cost=400)
    assert catalog.adjust_stock(db, pid, 8) == -2
    assert bal(db, "shrinkage") == 800
    assert bal(db, "inventory") == 3200


def test_duplicate_sku_rejected(db):
    catalog.create_product(db, Product(sku="A1", name="A"))
    with pytest.raises(catalog.CatalogError):
        catalog.create_product(db, Product(sku="A1", name="B"))


def test_csv_round_trip(db, tmp_path):
    make_product(db, name="Thing", price=1234)
    path = tmp_path / "p.csv"
    fmt = st.money_format(db)
    assert catalog.export_products_csv(db, path, fmt) == 1
    other = Database(":memory:")
    created, updated, errors = catalog.import_products_csv(other, path, fmt)
    assert (created, updated, errors) == (1, 0, [])
    assert catalog.list_products(other)[0].price == 1234
    assert catalog.import_products_csv(other, path, fmt)[1] == 1


# ---- sales ---------------------------------------------------------------------


def test_price_cart_discounts_and_tax():
    cart = sales.Cart([
        sales.CartLine(1, "A", "A", 1000, 2, discount_bps=1000),  # 2000 - 200
        sales.CartLine(2, "B", "B", 500, 1, taxable=False),
    ], order_discount=230)
    t = sales.price_cart(cart, 1000)
    assert t.subtotal == 2500
    assert t.discount == 430
    assert t.net == 2070
    assert t.tax == percent_of(t.lines[0].net, 1000)
    assert t.lines[1].tax == 0


def test_checkout_posts_balanced_entry_and_moves_stock(db):
    pid = make_product(db, price=1000, cost=400, stock=5)
    cart = sales.Cart()
    cart.add(catalog.get_product(db, pid), 2)
    sale_id = sales.checkout(db, cart, {"cash": 2200}, tendered_cash=3000)
    sale = sales.get_sale(db, sale_id)
    assert sale["total"] == 2200 and sale["change_given"] == 800
    assert catalog.get_product(db, pid).stock == 3
    assert bal(db, "cash") == 2200
    assert bal(db, "sales") == 2000
    assert bal(db, "tax") == 200
    assert bal(db, "cogs") == 800
    assert_books_balance(db)
    assert "Receipt #00001" in receipt_html(db, sale)


def test_split_payment_must_match_total(db):
    pid = make_product(db)
    cart = sales.Cart()
    cart.add(catalog.get_product(db, pid))
    with pytest.raises(sales.SaleError):
        sales.checkout(db, cart, {"cash": 500, "card": 500})
    sales.checkout(db, cart, {"cash": 600, "card": 500})
    assert bal(db, "card") == 500


def test_insufficient_stock_blocked_unless_allowed(db):
    pid = make_product(db, stock=1)
    cart = sales.Cart()
    cart.add(catalog.get_product(db, pid), 2)
    with pytest.raises(sales.SaleError):
        sales.checkout(db, cart, {"cash": 2200})
    st.set_many(db, {"allow_negative_stock": "1"})
    sales.checkout(db, cart, {"cash": 2200})
    assert catalog.get_product(db, pid).stock == -1


def test_on_account_sale_and_customer_payment(db):
    pid = make_product(db)
    cust = parties.save_party(db, "customers", Party(name="Office"))
    cart = sales.Cart()
    cart.add(catalog.get_product(db, pid))
    with pytest.raises(sales.SaleError):
        sales.checkout(db, cart, {"account": 1100})
    cart.customer_id = cust
    sales.checkout(db, cart, {"account": 1100})
    assert parties.customer_balance(db, cust) == 1100
    parties.receive_customer_payment(db, cust, 1100, "bank")
    assert parties.customer_balance(db, cust) == 0
    assert bal(db, "ar") == 0
    assert parties.customer_statement(db, cust)[-1]["balance"] == 0


def test_partial_then_full_refund(db):
    pid = make_product(db, price=333, cost=100, stock=10)
    cart = sales.Cart()
    cart.add(catalog.get_product(db, pid), 3)
    sale_id = sales.checkout(db, cart, {"card": 1099})  # 999 + 100 tax
    item_id = sales.get_sale(db, sale_id)["items"][0]["id"]

    sales.refund(db, sale_id, {item_id: 1}, "card")
    assert sales.get_sale(db, sale_id)["status"] == "partially_refunded"
    with pytest.raises(sales.SaleError):
        sales.refund(db, sale_id, {item_id: 3}, "card")
    sales.refund(db, sale_id, {item_id: 2}, "card", restock=False)
    sale = sales.get_sale(db, sale_id)
    assert sale["status"] == "refunded"
    assert sale["refunded_total"] == 1099  # exact, no rounding drift
    assert bal(db, "card") == 0
    assert bal(db, "tax") == 0
    assert catalog.get_product(db, pid).stock == 8  # only first return restocked
    assert_books_balance(db)


def test_held_cart_round_trip(db):
    pid = make_product(db)
    cart = sales.Cart(order_discount=50, note="table 4")
    cart.add(catalog.get_product(db, pid), 2)
    hid = sales.hold_cart(db, cart, "Table 4")
    assert sales.list_held(db)[0]["items"] == 2
    restored = sales.take_held(db, hid)
    assert restored.lines[0].quantity == 2 and restored.note == "table 4"
    assert sales.list_held(db) == []


# ---- expenses, shifts, reports -------------------------------------------------


def test_expense_and_void(db):
    rent = db.scalar("SELECT id FROM accounts WHERE code = '6000'")
    eid = expenses.record_expense(db, rent, acc.account_id(db, "bank"), 50000, payee="Landlord")
    assert acc.account_balance(db, rent) == 50000
    expenses.void_expense(db, eid)
    assert acc.account_balance(db, rent) == 0
    with pytest.raises(expenses.ExpenseError):
        expenses.record_expense(db, acc.account_id(db, "cash"), acc.account_id(db, "bank"), 1)


def test_shift_over_short(db):
    pid = make_product(db)
    shifts.open_shift(db, 10000)
    with pytest.raises(shifts.ShiftError):
        shifts.open_shift(db, 0)
    cart = sales.Cart()
    cart.add(catalog.get_product(db, pid))
    sales.checkout(db, cart, {"cash": 1100})
    assert shifts.expected_cash(db, shifts.current_shift(db)) == 11100
    result = shifts.close_shift(db, 11000)
    assert result["difference"] == -100
    assert bal(db, "over_short") == 100
    assert shifts.current_shift(db) is None


def test_sample_data_and_reports_are_consistent(db):
    load_sample_data(db, days=10)
    assert_books_balance(db)
    start, end = "2000-01-01", "2999-12-31"
    summary = reports.sales_summary(db, start, end)
    pl = acc.profit_and_loss(db, start, end)
    # Revenue and COGS in the ledger match the sales analytics.
    assert pl["income"].total == summary["net_sales"]
    cogs_row = [r for r in pl["cogs"].rows if r.code == "5000"][0]
    assert cogs_row.amount == summary["cogs"]
    assert sum(r["net"] for r in reports.sales_by_product(db, start, end)) == summary["net_sales"]
    assert reports.dashboard(db)["month"]["count"] >= 0
    gl = acc.general_ledger(db, acc.account_id(db, "cash"), start, end)
    assert gl["closing"] == bal(db, "cash")
