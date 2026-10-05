"""Customer orders (delivery or pickup): the staff queue, and admin editing.

Staff with "handle orders" see their own branch's orders; owners/admins see
and manage every branch. Orders sent to the web app's order API land in the
shared data file, so the list refreshes itself while it's on screen.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QPlainTextEdit, QRadioButton, QSpinBox, QSplitter,
    QTableWidget, QTabBar, QTextBrowser, QVBoxLayout, QWidget,
)

from ..core import branches, catalog, orders
from ..core.db import today_text
from ..core.orders import FULFILLMENTS, STATUS_LABELS, OrderInput, OrderItemInput
from .common import (
    AppContext, DataTable, Page, button, confirm, date_edit, error, iso, muted, page_layout,
    print_preview, run_action,
)

REFRESH_MS = 30_000
TABS = [("open", "Open"), ("new", "New"), ("mine", "Taken by me"), ("completed", "Completed"),
        ("cancelled", "Cancelled"), ("all", "All")]


def wording(fulfillment: str) -> dict[str, str]:
    """Labels that depend on whether the customer receives or collects the order."""
    if fulfillment == "pickup":
        return {"date": "Pick up on", "done": "Mark collected", "done_title": "Order collected",
                "done_note": "Note (optional), e.g. who collected it:", "completed": "Collected"}
    return {"date": "Deliver on", "done": "Mark delivered", "done_title": "Order delivered",
            "done_note": "Delivery note (optional), e.g. who received it:", "completed": "Delivered"}


def status_label(order: dict) -> str:
    if order["status"] == "completed":
        return wording(order["fulfillment"])["completed"]
    return STATUS_LABELS.get(order["status"], order["status"])


class OrdersPage(Page):
    title = "Orders"
    permission = "handle_orders"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        layout = page_layout(self, "Customer orders")
        self.subtitle = muted()
        layout.addWidget(self.subtitle)

        self.tabs = QTabBar()
        for _, label in TABS:
            self.tabs.addTab(label)
        self.tabs.currentChanged.connect(self.refresh)
        layout.addWidget(self.tabs)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Customer, phone, reference or #")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.type = QComboBox()
        self.type.addItem("Delivery and pickup", None)
        self.type.addItem("Delivery only", "delivery")
        self.type.addItem("Pickup only", "pickup")
        self.type.currentIndexChanged.connect(self.refresh)
        self.due = QCheckBox("Due between")
        self.start, self.end = date_edit(), date_edit(QDate.currentDate().addDays(7).toPython())
        for w in (self.start, self.end):
            w.setEnabled(False)
            w.dateChanged.connect(self.refresh)
        self.due.toggled.connect(lambda on: (self.start.setEnabled(on), self.end.setEnabled(on),
                                             self.refresh()))
        self.branch = QComboBox()
        self.branch.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.type)
        bar.addWidget(self.due)
        bar.addWidget(self.start)
        bar.addWidget(QLabel("and"))
        bar.addWidget(self.end)
        bar.addWidget(self.branch)
        layout.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        self.table = DataTable(["#", "Type", "Due", "Customer", "Phone", "Items", "Total", "Status",
                                "Staff", "Branch"], right_align=(0, 5, 6), stretch=3)
        self.table.itemSelectionChanged.connect(self._show)
        self.table.sortByColumn(2, Qt.AscendingOrder)  # a queue: soonest due first
        split.addWidget(self.table)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        rv.addWidget(self.detail, 1)
        actions = QHBoxLayout()
        self.take_btn = button("Take order", self._take, primary=True)
        self.done_btn = button("Mark delivered", self._complete, primary=True)
        self.release_btn = button("Release", self._release)
        self.cancel_btn = button("Cancel order", self._cancel)
        for b in (self.take_btn, self.done_btn, self.release_btn, self.cancel_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        rv.addLayout(actions)
        admin = QHBoxLayout()
        self.print_btn = button("Print slip…", self._print)
        self.new_btn = button("New order…", self._new)
        self.edit_btn = button("Edit…", self._edit)
        self.delete_btn = button("Delete", self._delete)
        for b in (self.print_btn, self.new_btn, self.edit_btn, self.delete_btn):
            admin.addWidget(b)
        admin.addStretch(1)
        rv.addLayout(admin)
        split.addWidget(right)
        split.setSizes([700, 420])
        layout.addWidget(split, 1)
        self._order: dict | None = None

        # Orders from the web app's order API arrive in the shared file.
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(lambda: self.isVisible() and self.refresh())
        self.timer.start()

    # ---- data ---------------------------------------------------------------------

    def manages(self) -> bool:
        return self.ctx.can("orders")

    def refresh(self) -> None:
        manage = self.manages()
        self.subtitle.setText("Delivery and pickup, all branches" if manage
                              else "Delivery and pickup orders for your branch")
        self.new_btn.setVisible(manage)
        self.edit_btn.setVisible(manage)
        self.delete_btn.setVisible(manage)
        self.branch.setVisible(manage)
        self.table.setColumnHidden(9, not manage)
        if manage:
            current = self.branch.currentData()
            self.branch.blockSignals(True)
            self.branch.clear()
            self.branch.addItem("All branches", None)
            for b in branches.list_branches(self.db):
                self.branch.addItem(b.name, b.id)
            self.branch.setCurrentIndex(max(0, self.branch.findData(current)))
            self.branch.blockSignals(False)

        key = TABS[self.tabs.currentIndex()][0]
        rows = orders.list_orders(
            self.db,
            self.branch.currentData() if manage else branches.branch_of_user(self.db, self.ctx.user),
            status="picked_up" if key == "mine" else key,
            fulfillment=self.type.currentData(),
            picked_by=self.ctx.user.id if key == "mine" else None,
            start=iso(self.start) if self.due.isChecked() else None,
            end=iso(self.end) if self.due.isChecked() else None,
            search=self.search.text().strip(),
        )
        m = self.ctx.money
        today = today_text()
        colors = {}
        for n, r in enumerate(rows):
            if r["status"] == "new" and r["scheduled_date"] < today:
                colors[n] = QColor("#c0392b")  # overdue
            elif r["status"] == "cancelled":
                colors[n] = QColor("gray")
        self.table.set_rows([
            [(str(r["id"]), r["id"]), FULFILLMENTS[r["fulfillment"]], r["scheduled_date"],
             r["customer_name"], r["phone"], (str(r["item_count"]), r["item_count"]),
             (m(r["total"]), r["total"]), status_label(r), r["picked_by_name"] or "", r["branch"]]
            for r in rows], [r["id"] for r in rows], colors=colors)
        self._show()

    def _visible_order(self, order_id: int) -> dict | None:
        try:
            order = orders.get_order(self.db, order_id)
        except orders.OrderError:
            return None
        if not self.manages() and order["branch_id"] != branches.branch_of_user(self.db, self.ctx.user):
            return None
        return order

    def _show(self) -> None:
        oid = self.table.selected_key()
        self._order = self._visible_order(oid) if oid else None
        o = self._order
        manage = self.manages()
        mine = bool(o) and o["picked_by"] == self.ctx.user.id
        in_progress = bool(o) and o["status"] == "picked_up"
        self.take_btn.setVisible(bool(o) and o["status"] == "new")
        self.done_btn.setVisible(in_progress and (mine or manage))
        self.release_btn.setVisible(in_progress and (mine or manage))
        self.cancel_btn.setVisible(bool(o) and ((in_progress and (mine or manage))
                                                or (o["status"] == "new" and manage)))
        self.print_btn.setEnabled(bool(o))
        self.edit_btn.setEnabled(bool(o) and o["status"] in ("new", "picked_up"))
        self.delete_btn.setEnabled(bool(o))
        if not o:
            self.detail.setHtml("<p style='color:gray'>Select an order to see its details.</p>")
            return
        self.done_btn.setText(wording(o["fulfillment"])["done"])
        self.detail.setHtml(detail_html(o, self.ctx.money, manage))

    # ---- actions -----------------------------------------------------------------

    def _take(self) -> None:
        o = self._order
        if o:
            run_action(self, lambda: orders.pick_up(self.db, o["id"]))
            self.ctx.notify()

    def _release(self) -> None:
        o = self._order
        if o:
            run_action(self, lambda: orders.release(self.db, o["id"], self.manages()))
            self.ctx.notify()

    def _complete(self) -> None:
        o = self._order
        if not o:
            return
        w = wording(o["fulfillment"])
        note, ok = QInputDialog.getText(self, w["done_title"], w["done_note"])
        if ok:
            run_action(self, lambda: orders.complete(self.db, o["id"], note, self.manages()))
            self.ctx.notify()

    def _cancel(self) -> None:
        o = self._order
        if not o:
            return
        reason, ok = QInputDialog.getText(self, "Cancel order", "Why is this order being cancelled?")
        if not ok:
            return
        if not reason.strip():
            error(self, "Give a reason for cancelling")
            return
        run_action(self, lambda: orders.cancel(self.db, o["id"], reason, self.manages()))
        self.ctx.notify()

    def _print(self) -> None:
        if self._order:
            print_preview(self, slip_html(self._order, self.ctx.money))

    def _new(self) -> None:
        dialog = OrderDialog(self.ctx, None, self)
        if dialog.exec() == QDialog.Accepted:
            self.ctx.notify()
            self.table.select_key(dialog.order_id)

    def _edit(self) -> None:
        if self._order and OrderDialog(self.ctx, self._order, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _delete(self) -> None:
        o = self._order
        if o and confirm(self, f"Delete order #{o['id']} permanently? Cancelling keeps a record; "
                               "deleting doesn't."):
            run_action(self, lambda: orders.delete_order(self.db, o["id"]))
            self.ctx.notify()


# ---- HTML -----------------------------------------------------------------------------


def detail_html(o: dict, money, manage: bool) -> str:
    e = escape
    w = wording(o["fulfillment"])

    def row(label: str, value: str) -> str:
        return f"<tr><td style='color:gray; padding-right:12px'>{e(label)}</td><td>{value}</td></tr>"

    rows = [row("Customer", f"<b>{e(o['customer_name'])}</b>"), row("Phone", e(o["phone"]))]
    if o["fulfillment"] == "pickup":
        where = f"<b>{e(o['branch'])}</b>" + (f" · {e(o['branch_address'])}" if o["branch_address"] else "")
        rows.append(row("Collects at", where))
        if o["address"]:
            rows.append(row("Customer address", e(o["address"]).replace("\n", "<br>")))
    else:
        rows.append(row("Deliver to", e(o["address"]).replace("\n", "<br>")))
    rows.append(row(w["date"], f"<b>{e(o['scheduled_date'])}</b>"))
    rows.append(row("Ordered at", e(o["ordered_at"])))
    source = "order API" if o["source"] == "api" else "entered by hand"
    rows.append(row("Received", e(f"{o['received_at']} · {source}" + (f" · {o['branch']}" if manage else ""))))
    if o["reference"]:
        rows.append(row("Reference", e(o["reference"])))
    if o["notes"]:
        rows.append(row("Notes", e(o["notes"]).replace("\n", "<br>")))
    if o["picked_by_name"]:
        rows.append(row("Taken by", e(f"{o['picked_by_name']} · {o['picked_at']}")))
    if o["closed_at"]:
        label = w["completed"] if o["status"] == "completed" else "Cancelled"
        rows.append(row(label, e(f"{o['closed_by_name'] or ''} · {o['closed_at']}"
                                 + (f" — {o['status_note']}" if o["status_note"] else ""))))
    items = "".join(
        f"<tr><td>{e(i['name'])} <small style='color:gray'>{e(i['sku'])}</small></td>"
        f"<td align='right'>{i['quantity']}</td><td align='right'>{e(money(i['unit_price']))}</td>"
        f"<td align='right'>{e(money(i['unit_price'] * i['quantity']))}</td></tr>" for i in o["items"])
    return (
        f"<h3>Order #{o['id']} &nbsp; <small>{FULFILLMENTS[o['fulfillment']]} · {status_label(o)}</small></h3>"
        f"<table cellspacing='0' cellpadding='2'>{''.join(rows)}</table><br>"
        "<table width='100%' cellspacing='0' cellpadding='3' border='1' style='border-collapse:collapse'>"
        "<tr><th align='left'>Item</th><th align='right'>Qty</th><th align='right'>Price</th>"
        f"<th align='right'>Line total</th></tr>{items}</table>"
        f"<p align='right'><b>Total {e(money(o['total']))}</b><br>"
        "<small style='color:gray'>Prices when the order was received, before tax.</small></p>"
    )


def slip_html(o: dict, money) -> str:
    e = escape
    pickup = o["fulfillment"] == "pickup"
    items = "".join(f"<tr><td>{i['quantity']} ×</td><td>{e(i['name'])} <small>({e(i['sku'])})</small>"
                    "</td></tr>" for i in o["items"])
    where = (f"<p>Customer collects at <b>{e(o['branch'])}</b>"
             + (f"<br>{e(o['branch_address'])}" if o["branch_address"] else "") + "</p>") if pickup \
        else f"<p>{e(o['address']).replace(chr(10), '<br>')}</p>"
    return (
        "<html><body style='font-family: sans-serif; font-size: 11pt'>"
        f"<h2>{'PICKUP' if pickup else 'DELIVERY'} · Order #{o['id']}</h2>"
        f"<div>{e(o['branch'])}" + (f" · Ref {e(o['reference'])}" if o["reference"] else "") + "</div><hr>"
        f"<b>{e(o['customer_name'])}</b><br>{e(o['phone'])}{where}"
        f"<p>{'Pick up' if pickup else 'Deliver'} on <b>{e(o['scheduled_date'])}</b><br>"
        f"<small>Ordered {e(o['ordered_at'])}</small></p>"
        + (f"<p><b>Notes:</b> {e(o['notes'])}</p>" if o["notes"] else "")
        + f"<hr><table>{items}</table><hr>Total (before tax): <b>{e(money(o['total']))}</b>"
        "</body></html>"
    )


# ---- dialog -----------------------------------------------------------------------------


class OrderDialog(QDialog):
    """Admin: enter an order by hand (e.g. taken by phone), or edit an open one."""

    def __init__(self, ctx: AppContext, order: dict | None, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx, self.order = ctx, order
        self.order_id: int | None = order["id"] if order else None
        self.setWindowTitle(f"Edit order #{order['id']}" if order else "New customer order")
        self.resize(640, 620)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.delivery = QRadioButton("Delivery to the customer")
        self.pickup = QRadioButton("Customer picks up at the branch")
        (self.pickup if order and order["fulfillment"] == "pickup" else self.delivery).setChecked(True)
        types = QHBoxLayout()
        types.addWidget(self.delivery)
        types.addWidget(self.pickup)
        types.addStretch(1)
        form.addRow("Order type", types)
        self.branch = QComboBox()
        for b in branches.list_branches(ctx.db, include_inactive=False):
            self.branch.addItem(b.name, b.id)
        self.branch.setCurrentIndex(max(0, self.branch.findData(branches.current_branch(ctx.db).id)))
        if order is None:
            form.addRow("Branch", self.branch)
        o = order or {}
        self.name = QLineEdit(o.get("customer_name", ""))
        self.phone = QLineEdit(o.get("phone", ""))
        self.address = QPlainTextEdit(o.get("address", ""))
        self.address.setFixedHeight(60)
        self.date = date_edit()
        if order:
            self.date.setDate(QDate.fromString(order["scheduled_date"], "yyyy-MM-dd"))
        self.reference = QLineEdit(o.get("reference") or "")
        self.reference.setPlaceholderText("Optional, e.g. phone order number")
        self.notes = QPlainTextEdit(o.get("notes", ""))
        self.notes.setFixedHeight(60)
        self.address_label, self.date_label = QLabel(), QLabel()
        form.addRow("Customer name", self.name)
        form.addRow("Phone", self.phone)
        form.addRow(self.address_label, self.address)
        form.addRow(self.date_label, self.date)
        form.addRow("Reference", self.reference)
        form.addRow("Notes", self.notes)
        layout.addLayout(form)

        self.items = QTableWidget(0, 2)
        self.items.setHorizontalHeaderLabels(["Product", "Quantity"])
        self.items.verticalHeader().setVisible(False)
        self.items.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(self.items, 1)
        row = QHBoxLayout()
        row.addWidget(button("Add item", lambda: self._add_item()))
        row.addWidget(button("Remove item", self._remove_item))
        row.addStretch(1)
        layout.addLayout(row)
        self.products = catalog.list_products(ctx.db)
        for item in (order["items"] if order else []):
            self._add_item(item["product_id"], item["quantity"])
        if not order:
            self._add_item()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.delivery.toggled.connect(self._update_labels)
        self._update_labels()

    def _update_labels(self) -> None:
        pickup = self.pickup.isChecked()
        self.address_label.setText("Customer address" if pickup else "Delivery address")
        self.address.setPlaceholderText("Optional for pickup" if pickup else "")
        self.date_label.setText(wording("pickup" if pickup else "delivery")["date"])

    def _add_item(self, product_id: int | None = None, quantity: int = 1) -> None:
        r = self.items.rowCount()
        self.items.insertRow(r)
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        for p in self.products:
            combo.addItem(f"{p.name}  [{p.sku}]", p.id)
        combo.completer().setFilterMode(Qt.MatchContains)
        combo.setCurrentIndex(combo.findData(product_id) if product_id else -1)
        qty = QSpinBox()
        qty.setRange(1, 1_000_000)
        qty.setValue(quantity)
        self.items.setCellWidget(r, 0, combo)
        self.items.setCellWidget(r, 1, qty)

    def _remove_item(self) -> None:
        r = self.items.currentRow()
        self.items.removeRow(r if r >= 0 else self.items.rowCount() - 1)

    def _input(self) -> OrderInput:
        items = []
        for r in range(self.items.rowCount()):
            pid = self.items.cellWidget(r, 0).currentData()
            if pid is not None:
                items.append(OrderItemInput(self.items.cellWidget(r, 1).value(), product_id=pid))
        return OrderInput(
            customer_name=self.name.text(), address=self.address.toPlainText(),
            phone=self.phone.text(), scheduled_date=iso(self.date), items=items,
            reference=self.reference.text(), notes=self.notes.toPlainText(),
            fulfillment="pickup" if self.pickup.isChecked() else "delivery")

    def _save(self) -> None:
        data = self._input()
        if self.order:
            ok = run_action(self, lambda: orders.update_order(self.ctx.db, self.order["id"], data) or True)
        else:
            result = run_action(self, lambda: orders.receive_order(
                self.ctx.db, self.branch.currentData(), data, "manual"))
            ok = bool(result)
            if result:
                self.order_id, duplicate = result
                if duplicate:
                    error(self, "An order with that reference already exists")
                    return
        if ok:
            self.accept()
