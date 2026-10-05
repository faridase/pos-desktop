"""Headless UI smoke tests: every screen renders and the main flows work
through the real widgets."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from pos_desktop.core import accounting as acc  # noqa: E402
from pos_desktop.core import auth, catalog, sales  # noqa: E402
from pos_desktop.core.db import Database  # noqa: E402
from pos_desktop.core.sample_data import load_sample_data  # noqa: E402
from pos_desktop.ui import accounting_page, expenses_page, inventory_page, parties_page  # noqa: E402
from pos_desktop.ui import products_page, register, sales_page  # noqa: E402
from pos_desktop.ui.common import AppContext  # noqa: E402
from pos_desktop.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    db = Database(":memory:")
    load_sample_data(db, days=5)
    auth.create_user(db, "owner", "Olivia Owner", "admin", "secret99")
    auth.create_user(db, "maya", "Maya Manager", "manager", "manager1")
    auth.create_user(db, "carl", "Carl Cashier", "cashier", "1234")
    ctx = AppContext(db)
    ctx.login(auth.authenticate(db, "owner", "secret99"))
    w = MainWindow(ctx)
    w.show()
    yield w
    w.close()
    db.close()


def page_index(window, title):
    """Pages are found by title so adding screens doesn't break tests."""
    return next(n for n, p in enumerate(window.pages) if p.title == title)


def test_every_page_and_tab_renders(window, app):
    for i, page in enumerate(window.pages):
        window.nav.setCurrentRow(i)
        app.processEvents()
        assert window.stack.currentWidget() is page
        for tabs in page.findChildren(QTabWidget):
            for t in range(tabs.count()):
                tabs.setCurrentIndex(t)
                app.processEvents()


def test_report_groupings(window, app):
    n = page_index(window, "Reports")
    reports = window.pages[n]
    window.nav.setCurrentRow(n)
    sales_tab = reports.reports[0][0]
    for g in range(sales_tab.group.count()):
        sales_tab.group.setCurrentIndex(g)
        app.processEvents()
        assert sales_tab.table.rowCount() >= 1


def test_register_checkout_flow(window, app):
    ctx = window.ctx
    reg: register.RegisterPage = window.pages[1]
    window.nav.setCurrentRow(1)
    app.processEvents()

    # Scan by SKU, then bump quantity and apply a 10% order discount.
    product = next(p for p in catalog.list_products(ctx.db) if p.track_stock and p.stock > 5)
    reg.search.setText(product.sku)
    reg._scan()
    reg._bump(1)
    reg.discount_type.setCurrentText("%")
    reg.discount.setText("10")
    reg._render()
    assert reg.cart.lines[0].quantity == 2
    totals = reg.totals()
    assert totals.discount > 0

    dialog = register.CheckoutDialog(ctx, reg.cart, totals, reg)
    dialog.cash.set_value(totals.total + 500)
    assert dialog.complete.isEnabled()
    before = len(sales.list_sales(ctx.db, "2000-01-01", "2999-12-31"))
    dialog._complete()
    assert dialog.sale_id
    assert len(sales.list_sales(ctx.db, "2000-01-01", "2999-12-31")) == before + 1
    sale = sales.get_sale(ctx.db, dialog.sale_id)
    assert sale["change_given"] == 500
    register.ReceiptDialog(ctx, dialog.sale_id, reg)

    # Hold / recall round trip.
    reg._reset()
    reg._add(product.id)
    sales.hold_cart(ctx.db, reg.cart, "x")
    held = register.HeldOrdersDialog(ctx, reg)
    assert held.table.rowCount() == 1


def test_dialogs_construct(window):
    ctx = window.ctx
    db = ctx.db
    pid = catalog.list_products(db)[0].id
    sale_id = sales.list_sales(db, "2000-01-01", "2999-12-31")[0]["id"]
    customer = db.scalar("SELECT id FROM customers LIMIT 1")
    parent = window
    products_page.ProductDialog(ctx, None, parent)
    products_page.ProductDialog(ctx, catalog.get_product(db, pid), parent)
    inventory_page.PurchaseDialog(ctx, parent)._add_line()
    inventory_page.AdjustDialog(ctx, pid, parent)
    sales_page.RefundDialog(ctx, sale_id, parent)
    parties_page.PartyDialog(ctx, "customers", None, parent)
    parties_page.PaymentDialog(ctx, "customers", customer, 0, parent)
    expenses_page.ExpenseDialog(ctx, parent)
    accounting_page.AccountDialog(ctx, None, parent)
    accounting_page.JournalEntryDialog(ctx, parent)
    accounting_page.TransferDialog(ctx, parent)


def test_manual_journal_entry_dialog_posts(window):
    ctx = window.ctx
    dialog = accounting_page.JournalEntryDialog(ctx, window)
    dialog.memo.setText("Owner top-up")
    t = dialog.table
    t.cellWidget(0, 0).setCurrentIndex(t.cellWidget(0, 0).findData(acc.account_id(ctx.db, "cash")))
    t.cellWidget(0, 1).set_value(1000)
    t.cellWidget(1, 0).setCurrentIndex(t.cellWidget(1, 0).findData(acc.account_id(ctx.db, "equity")))
    t.cellWidget(1, 2).set_value(1000)
    assert dialog.ok.isEnabled()
    before = acc.account_balance(ctx.db, acc.account_id(ctx.db, "cash"))
    dialog._save()
    assert acc.account_balance(ctx.db, acc.account_id(ctx.db, "cash")) == before + 1000


def test_refund_dialog_refunds(window):
    ctx = window.ctx
    sale_id = sales.list_sales(ctx.db, "2000-01-01", "2999-12-31")[0]["id"]
    dialog = sales_page.RefundDialog(ctx, sale_id, window)
    dialog._save()
    assert sales.get_sale(ctx.db, sale_id)["status"] == "refunded"


# ---- staff sign-in and roles ------------------------------------------------------


def visible_pages(window):
    return [p.title for n, p in enumerate(window.pages) if not window.nav.isRowHidden(n)]


def test_role_filters_screens(window, app):
    ctx = window.ctx
    assert len(visible_pages(window)) == len(window.pages)  # owner sees everything
    ctx.login(auth.authenticate(ctx.db, "carl", "1234"))
    app.processEvents()
    assert visible_pages(window) == ["Register", "Sales"]
    assert window.stack.currentWidget().title == "Register"
    assert "Carl Cashier" in window.user_label.text()
    backup_actions = [a for a in window.gated_actions if a.data() == "backup"]
    assert backup_actions and not any(a.isVisible() for a in backup_actions)
    ctx.login(auth.authenticate(ctx.db, "maya", "manager1"))
    pages = visible_pages(window)
    assert "Reports" in pages and "Accounting" not in pages and "Settings" not in pages


def test_cashier_discount_needs_manager_approval(window, monkeypatch):
    ctx = window.ctx
    ctx.login(auth.authenticate(ctx.db, "carl", "1234"))
    reg = window.pages[1]
    reg._reset()
    product = next(p for p in catalog.list_products(ctx.db) if p.track_stock and p.stock > 2)
    reg._add(product.id)

    # Refused: the discount is not applied.
    monkeypatch.setattr(register, "request_permission", lambda *a: None)
    reg.table.item(0, 3).setText("50")
    assert reg.cart.lines[0].discount_bps == 0

    # Approved by the manager: applied, and recorded on the sale.
    manager = auth.authenticate(ctx.db, "maya", "manager1", purpose="override")
    monkeypatch.setattr(register, "request_permission", lambda *a: manager.id)
    reg.table.item(0, 3).setText("50")
    assert reg.cart.lines[0].discount_bps == 5000
    totals = reg.totals()
    dialog = register.CheckoutDialog(ctx, reg.cart, totals, reg, reg.discount_approver)
    dialog._complete()
    sale = sales.get_sale(ctx.db, dialog.sale_id)
    assert sale["cashier"] == "Carl Cashier" and sale["approver"] == "Maya Manager"


def test_cashier_refund_needs_approval(window, monkeypatch):
    ctx = window.ctx
    ctx.login(auth.authenticate(ctx.db, "carl", "1234"))
    n = page_index(window, "Sales")
    page = window.pages[n]
    window.nav.setCurrentRow(n)
    page.refresh()
    page.table.selectRow(0)
    opened = []
    monkeypatch.setattr(sales_page, "request_permission", lambda *a: None)
    monkeypatch.setattr(sales_page.RefundDialog, "exec", lambda self: opened.append(self) or 0)
    page._refund()
    assert opened == []  # no approval, no refund dialog
    manager = auth.authenticate(ctx.db, "maya", "manager1", purpose="override")
    monkeypatch.setattr(sales_page, "request_permission", lambda *a: manager.id)
    page._refund()
    assert opened and opened[0].approved_by == manager.id


def test_login_and_setup_dialogs(app):
    from pos_desktop.ui.auth_dialogs import LoginDialog, SetupOwnerDialog
    db = Database(":memory:")
    setup = SetupOwnerDialog(db)
    setup.full_name.setText("Olivia")
    setup.password.setText("secret99")
    setup.confirm.setText("secret98")
    setup.submit()
    assert setup.user is None and setup.error.isVisibleTo(setup)
    setup.confirm.setText("secret99")
    setup.submit()
    assert setup.user and setup.user.role == "admin"

    login = LoginDialog(db)
    login.username.setText("owner")
    login.password.setText("wrong!")
    login.submit()
    assert login.user is None and "Wrong" in login.error.text()
    login.password.setText("secret99")
    login.submit()
    assert login.user.username == "owner"


def test_staff_page_and_user_dialog(window, app):
    from pos_desktop.ui.auth_dialogs import UserDialog
    n = page_index(window, "Staff")
    page = window.pages[n]
    window.nav.setCurrentRow(n)
    app.processEvents()
    assert page.table.rowCount() == 3
    dialog = UserDialog(window.ctx, None, page)
    dialog.full_name.setText("Tina Till")
    dialog.username.setText("tina")
    dialog.role.setCurrentIndex(dialog.role.findData("cashier"))
    dialog.password.setText("2468")
    dialog.confirm.setText("2468")
    dialog.submit()
    assert auth.authenticate(window.ctx.db, "tina", "2468").role == "cashier"
    page.tabs.setCurrentIndex(1)
    page._refresh_audit()
    assert page.audit.rowCount() > 0


# ---- branches and customer orders ------------------------------------------------------


def test_branches_page_and_api_key(window, app, monkeypatch):
    from pos_desktop.core import branches
    from pos_desktop.ui import branches_page
    n = page_index(window, "Branches")
    page = window.pages[n]
    window.nav.setCurrentRow(n)
    app.processEvents()
    assert page.table.rowCount() == 1 and "(head)" in page.table.item(0, 1).text()
    dialog = branches_page.BranchDialog(window.ctx, None, page)
    dialog.code.setText("north")
    dialog.name.setText("North Store")
    dialog._save()
    page.refresh()
    assert page.table.rowCount() == 2
    north = next(b for b in branches.list_branches(window.ctx.db) if b.code == "NORTH")
    page.table.select_key(north.id)
    shown = []
    monkeypatch.setattr(branches_page.ApiKeyDialog, "exec", lambda self: shown.append(self.key.text()))
    page._generate()
    assert shown and branches.branch_for_api_key(window.ctx.db, shown[0]).id == north.id


def test_orders_page_take_and_complete(window, app, monkeypatch):
    from pos_desktop.core import branches, orders
    from pos_desktop.core.orders import OrderInput, OrderItemInput
    from pos_desktop.ui import orders_page
    db = window.ctx.db
    head = branches.head_branch(db).id
    sku = catalog.list_products(db)[0].sku
    delivery, _ = orders.receive_order(db, head, OrderInput(
        "Dewi", "Jl. Merdeka 1", "0812", "2999-01-01", [OrderItemInput(2, sku=sku)], "SHOP-1"), "api")
    pickup, _ = orders.receive_order(db, head, OrderInput(
        "Budi", "", "0813", "2999-01-01", [OrderItemInput(1, sku=sku)], fulfillment="pickup"), "api")

    # A cashier who handles orders sees them; one who doesn't, doesn't.
    auth.create_user(db, "ana", "Ana Rider", "cashier", "1234", handles_orders=True)
    window.ctx.login(auth.authenticate(db, "carl", "1234"))
    assert "Orders" not in visible_pages(window)
    window.ctx.login(auth.authenticate(db, "ana", "1234"))
    assert visible_pages(window) == ["Register", "Orders", "Sales"]
    n = page_index(window, "Orders")
    assert window.nav.item(n).text() == "Orders (2)"
    page = window.pages[n]
    window.nav.setCurrentRow(n)
    app.processEvents()
    assert page.table.rowCount() == 2
    assert not page.new_btn.isVisible()  # admin-only

    page.table.select_key(pickup)
    assert "Collects at" in page.detail.toHtml()
    page._take()
    page.table.select_key(pickup)
    assert page.done_btn.text() == "Mark collected"
    monkeypatch.setattr(orders_page.QInputDialog, "getText", lambda *a: ("Collected by Budi", True))
    page._complete()
    done = orders.get_order(db, pickup)
    assert done["status"] == "completed" and done["closed_by_name"] == "Ana Rider"
    assert window.nav.item(n).text() == "Orders (1)"

    # Admin: enter an order by hand through the dialog.
    window.ctx.login(auth.authenticate(db, "owner", "secret99"))
    dialog = orders_page.OrderDialog(window.ctx, None, page)
    dialog.pickup.setChecked(True)
    dialog.name.setText("Rudi")
    dialog.phone.setText("0814")
    dialog.items.cellWidget(0, 0).setCurrentIndex(0)
    dialog._save()
    assert dialog.order_id and orders.get_order(db, dialog.order_id)["fulfillment"] == "pickup"
    assert orders.get_order(db, delivery)["status"] == "new"


def test_register_stamps_the_running_branch(window, app):
    from pos_desktop.core import branches
    db = window.ctx.db
    reg = window.pages[page_index(window, "Register")]
    reg._reset()
    product = next(p for p in catalog.list_products(db) if p.track_stock and p.stock > 1)
    reg._add(product.id)
    dialog = register.CheckoutDialog(window.ctx, reg.cart, reg.totals(), reg)
    dialog._complete()
    assert db.scalar("SELECT branch_id FROM sales WHERE id = ?", (dialog.sale_id,)) ==         branches.head_branch(db).id
