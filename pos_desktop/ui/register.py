"""Point of sale screen."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QSplitter, QTableWidget, QTableWidgetItem,
    QTextBrowser, QVBoxLayout, QWidget,
)

from ..core import branches, catalog, parties, sales, shifts
from ..core import settings as st
from ..core.money import format_bps, parse_bps, percent_of
from ..core.receipts import receipt_html
from .auth_dialogs import request_permission
from .common import (
    RIGHT, AppContext, DataTable, MoneyEdit, Page, button, confirm, error, heading,
    info, muted, print_direct, print_preview, run_action, save_pdf,
)


def branch_id(db) -> int:
    """The branch this till runs as (for now always the head branch)."""
    return branches.current_branch(db).id


class RegisterPage(Page):
    title = "Register"
    permission = "register"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.cart = sales.Cart()
        self._rendering = False
        # Supervisor who approved a discount/price change on this order (only
        # set when the cashier lacked the permission themselves).
        self.discount_approver: int | None = None
        self._discount_ok = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)

        # Top bar: shift + held orders.
        top = QHBoxLayout()
        self.store_label = heading("", 15)
        top.addWidget(self.store_label)
        top.addStretch(1)
        self.shift_label = muted()
        top.addWidget(self.shift_label)
        self.shift_button = button("Open shift", self._toggle_shift)
        top.addWidget(self.shift_button)
        self.held_button = button("Held orders", self._recall)
        top.addWidget(self.held_button)
        root.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_catalog())
        splitter.addWidget(self._build_cart())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([700, 460])
        root.addWidget(splitter, 1)

        for key, slot in (("F2", self.search.setFocus), ("F8", self._hold),
                          ("F12", self._charge), ("Ctrl+Return", self._charge)):
            QShortcut(QKeySequence(key), self, slot)
        QShortcut(QKeySequence(Qt.Key_Delete), self.table, self._remove_line)

    # ---- layout -----------------------------------------------------------------

    def _build_catalog(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 6, 0)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Scan barcode or search products  (F2)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._fill_products)
        self.search.returnPressed.connect(self._scan)
        self.category = QComboBox()
        self.category.setMinimumWidth(140)
        self.category.currentIndexChanged.connect(self._fill_products)
        row.addWidget(self.search, 1)
        row.addWidget(self.category)
        layout.addLayout(row)

        self.products = QListWidget()
        self.products.setViewMode(QListWidget.IconMode)
        self.products.setResizeMode(QListWidget.Adjust)
        self.products.setMovement(QListWidget.Static)
        self.products.setUniformItemSizes(True)
        self.products.setGridSize(QSize(150, 78))
        self.products.setSpacing(6)
        self.products.setWordWrap(True)
        self.products.setObjectName("productGrid")
        self.products.itemClicked.connect(self._product_clicked)
        self.products.itemActivated.connect(self._product_clicked)
        layout.addWidget(self.products, 1)
        return w

    def _build_cart(self) -> QWidget:
        w = QFrame()
        w.setObjectName("card")
        layout = QVBoxLayout(w)

        row = QHBoxLayout()
        row.addWidget(QLabel("Customer"))
        self.customer = QComboBox()
        self.customer.currentIndexChanged.connect(self._customer_changed)
        row.addWidget(self.customer, 1)
        layout.addLayout(row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Item", "Qty", "Price", "Disc %", "Total"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.AnyKeyPressed)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 5):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._cell_edited)
        self.table.setToolTip("Double-click Qty, Price or Disc % to edit")
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        for text, slot in (("−", lambda: self._bump(-1)), ("+", lambda: self._bump(1)),
                           ("Remove", self._remove_line), ("Clear", self._clear)):
            row.addWidget(button(text, slot))
        row.addStretch(1)
        layout.addLayout(row)

        form = QGridLayout()
        form.addWidget(QLabel("Order discount"), 0, 0)
        self.discount = QLineEdit()
        self.discount.setAlignment(RIGHT)
        self.discount.setPlaceholderText("0")
        self.discount.editingFinished.connect(self._order_discount_edited)
        self.discount_type = QComboBox()
        self.discount_type.addItems(["Amount", "%"])
        self.discount_type.currentIndexChanged.connect(self._render)
        form.addWidget(self.discount, 0, 1)
        form.addWidget(self.discount_type, 0, 2)
        form.addWidget(QLabel("Note"), 1, 0)
        self.note = QLineEdit()
        self.note.setPlaceholderText("Optional, printed on receipt")
        form.addWidget(self.note, 1, 1, 1, 2)
        layout.addLayout(form)

        totals = QFormLayout()
        totals.setLabelAlignment(Qt.AlignLeft)
        self.subtotal_label, self.discount_label, self.tax_label = QLabel(), QLabel(), QLabel()
        self.tax_title = QLabel("Tax")
        for lbl in (self.subtotal_label, self.discount_label, self.tax_label):
            lbl.setAlignment(RIGHT)
        totals.addRow("Subtotal", self.subtotal_label)
        totals.addRow("Discount", self.discount_label)
        totals.addRow(self.tax_title, self.tax_label)
        self.total_label = heading("", 22)
        self.total_label.setAlignment(RIGHT)
        totals.addRow(heading("Total", 14), self.total_label)
        layout.addLayout(totals)

        row = QHBoxLayout()
        row.addWidget(button("Hold  (F8)", self._hold))
        self.charge_button = button("Charge  (F12)", self._charge, primary=True)
        self.charge_button.setMinimumHeight(48)
        row.addWidget(self.charge_button, 1)
        layout.addLayout(row)
        return w

    # ---- data -----------------------------------------------------------------

    def refresh(self) -> None:
        self.store_label.setText(st.get(self.db, "store_name"))
        current = self.category.currentData()
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("All categories", None)
        for c in catalog.categories(self.db):
            self.category.addItem(c, c)
        self.category.setCurrentIndex(max(0, self.category.findData(current)))
        self.category.blockSignals(False)

        selected = self.cart.customer_id
        self.customer.blockSignals(True)
        self.customer.clear()
        self.customer.addItem("Walk-in customer", None)
        for c in parties.list_parties(self.db, "customers"):
            self.customer.addItem(c.name, c.id)
        self.customer.setCurrentIndex(max(0, self.customer.findData(selected)))
        self.customer.blockSignals(False)

        shift = shifts.current_shift(self.db, branch_id(self.db))
        if shift:
            self.shift_label.setText(f"Shift #{shift['id']} open since {shift['opened_at'][11:16]}")
            self.shift_button.setText("Close shift…")
        else:
            self.shift_label.setText("No shift open")
            self.shift_button.setText("Open shift…")
        held = len(sales.list_held(self.db, branch_id(self.db)))
        self.held_button.setText(f"Held orders ({held})" if held else "Held orders")
        self._fill_products()
        self._render()

    def _fill_products(self) -> None:
        items = catalog.list_products(self.db, self.search.text().strip(),
                                      self.category.currentData())
        self.products.clear()
        for p in items:
            text = f"{p.name}\n{self.ctx.money(p.price)}"
            if p.track_stock:
                text += f"  ·  {p.stock} left" if p.stock <= max(p.reorder_level, 5) else ""
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, p.id)
            item.setTextAlignment(Qt.AlignCenter)
            item.setSizeHint(QSize(144, 72))
            item.setToolTip(f"{p.name}\nSKU {p.sku}" + (f"\nIn stock: {p.stock}" if p.track_stock else ""))
            if p.track_stock and p.stock <= 0:
                item.setForeground(QColor("#c0392b"))
            self.products.addItem(item)

    def _add(self, product_id: int) -> None:
        p = catalog.get_product(self.db, product_id)
        line = self.cart.add(p)
        self._render(select=self.cart.lines.index(line))

    def _product_clicked(self, item: QListWidgetItem) -> None:
        self._add(item.data(Qt.UserRole))

    def _scan(self) -> None:
        code = self.search.text().strip()
        if not code:
            return
        p = catalog.find_by_code(self.db, code)
        if p is None and self.products.count() == 1:
            p = catalog.get_product(self.db, self.products.item(0).data(Qt.UserRole))
        if p is None:
            self.window().statusBar().showMessage(f"No product matches “{code}”", 4000)
            return
        self._add(p.id)
        self.search.clear()

    def _customer_changed(self) -> None:
        self.cart.customer_id = self.customer.currentData()

    # ---- cart editing ---------------------------------------------------------

    def _order_discount(self, net_before: int) -> int:
        text = self.discount.text().strip()
        if not text:
            return 0
        if self.discount_type.currentText() == "%":
            bps = parse_bps(text)
            return percent_of(net_before, bps) if bps is not None else 0
        return self.ctx.fmt.parse(text) or 0

    def totals(self) -> sales.CartTotals:
        rate = st.get_int(self.db, "tax_rate_bps")
        self.cart.order_discount = 0
        before = sales.price_cart(self.cart, rate).net
        self.cart.order_discount = self._order_discount(before)
        self.cart.note = self.note.text().strip()
        return sales.price_cart(self.cart, rate)

    def _render(self, select: int | None = None) -> None:
        t = self.totals()
        m = self.ctx.money
        row = self.table.currentRow() if select is None else select
        self._rendering = True
        self.table.setRowCount(len(t.lines))
        for r, pl in enumerate(t.lines):
            cells = [pl.line.name, str(pl.line.quantity), self.ctx.fmt.to_input(pl.line.unit_price),
                     format_bps(pl.line.discount_bps).rstrip("%") if pl.line.discount_bps else "",
                     m(pl.net)]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c in (0, 4):
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if c > 0:
                    item.setTextAlignment(RIGHT)
                self.table.setItem(r, c, item)
        self._rendering = False
        if 0 <= row < len(t.lines):
            self.table.selectRow(row)
        self.subtotal_label.setText(m(t.subtotal))
        self.discount_label.setText("-" + m(t.discount) if t.discount else m(0))
        rate = st.get_int(self.db, "tax_rate_bps")
        self.tax_title.setText(f"{st.get(self.db, 'tax_label')} ({format_bps(rate)})")
        self.tax_label.setText(m(t.tax))
        self.total_label.setText(m(t.total))
        self.charge_button.setEnabled(bool(self.cart.lines))

    def _cell_edited(self, item: QTableWidgetItem) -> None:
        if self._rendering:
            return
        line = self.cart.lines[item.row()]
        text = item.text().strip()
        if item.column() == 1:
            try:
                qty = int(text)
            except ValueError:
                qty = line.quantity
            if qty <= 0:
                self.cart.lines.remove(line)
            else:
                line.quantity = qty
        elif item.column() == 2:
            price = self.ctx.fmt.parse(text)
            if price is not None and price != line.unit_price and                     self._approve_discount(f"Change the price of {line.name}"):
                line.unit_price = price
        elif item.column() == 3:
            bps = parse_bps(text or "0")
            if bps is not None and bps != line.discount_bps and                     self._approve_discount(f"Discount {line.name}"):
                line.discount_bps = bps
        self._render()

    def _approve_discount(self, action: str) -> bool:
        """Discounts and price changes need the "discount" permission, or a
        manager's approval once per order."""
        if self._discount_ok:
            return True
        approver = request_permission(self, self.ctx, "discount", action)
        if approver is None:
            return False
        self._discount_ok = True
        if approver != self.ctx.user.id:
            self.discount_approver = approver
        return True

    def _order_discount_edited(self) -> None:
        text = self.discount.text().strip()
        if text and text not in ("0", "0%") and not self._discount_ok:
            self.discount.blockSignals(True)
            if not self._approve_discount("Apply an order discount"):
                self.discount.clear()
            self.discount.blockSignals(False)
        self._render()

    def _bump(self, delta: int) -> None:
        r = self.table.currentRow()
        if not 0 <= r < len(self.cart.lines):
            return
        line = self.cart.lines[r]
        line.quantity += delta
        if line.quantity <= 0:
            self.cart.lines.pop(r)
            r = min(r, len(self.cart.lines) - 1)
        self._render(select=r)

    def _remove_line(self) -> None:
        r = self.table.currentRow()
        if 0 <= r < len(self.cart.lines):
            self.cart.lines.pop(r)
            self._render(select=min(r, len(self.cart.lines) - 1))

    def _reset(self) -> None:
        self.cart = sales.Cart()
        self.discount_approver = None
        self._discount_ok = False
        self.discount.clear()
        self.note.clear()
        self.customer.setCurrentIndex(0)
        self._render()
        self.search.setFocus()

    def _clear(self) -> None:
        if self.cart.lines and confirm(self, "Clear the current order?"):
            self._reset()

    # ---- actions ----------------------------------------------------------------

    def _charge(self) -> None:
        if not self.cart.lines:
            return
        totals = self.totals()
        dialog = CheckoutDialog(self.ctx, self.cart, totals, self, self.discount_approver)
        if dialog.exec() != QDialog.Accepted or dialog.sale_id is None:
            return
        sale_id = dialog.sale_id
        self._reset()
        self.ctx.notify()
        ReceiptDialog(self.ctx, sale_id, self, title="Sale complete").exec()

    def _hold(self) -> None:
        if not self.cart.lines:
            return
        self.totals()
        from PySide6.QtWidgets import QInputDialog
        label, ok = QInputDialog.getText(self, "Hold order", "Label (e.g. table or customer name):")
        if not ok:
            return
        if run_action(self, lambda: sales.hold_cart(self.db, self.cart, label.strip(),
                                                    branch_id(self.db))):
            self._reset()
            self.ctx.notify()

    def _recall(self) -> None:
        dialog = HeldOrdersDialog(self.ctx, self)
        if dialog.exec() != QDialog.Accepted or dialog.held_id is None:
            return
        if self.cart.lines:
            if not confirm(self, "Replace the current order with the held one? "
                                 "The current order will be held instead."):
                return
            self.totals()
            sales.hold_cart(self.db, self.cart, "Swapped", branch_id(self.db))
        cart = sales.take_held(self.db, dialog.held_id, branch_id(self.db))
        self._reset()
        self.cart = cart
        self.note.setText(cart.note)
        if cart.order_discount:
            self.discount_type.setCurrentText("Amount")
            self.discount.setText(self.ctx.fmt.to_input(cart.order_discount))
        self.customer.setCurrentIndex(max(0, self.customer.findData(cart.customer_id)))
        self.ctx.notify()

    def _toggle_shift(self) -> None:
        if shifts.current_shift(self.db, branch_id(self.db)):
            CloseShiftDialog(self.ctx, self).exec()
        else:
            dialog = QDialog(self)
            dialog.setWindowTitle("Open shift")
            form = QFormLayout(dialog)
            amount = MoneyEdit(self.ctx, 0)
            form.addRow(muted("Count the cash in the drawer before you start selling."))
            form.addRow("Opening float", amount)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            form.addRow(buttons)
            if dialog.exec() == QDialog.Accepted:
                value = amount.value()
                if value is None:
                    error(self, "Enter a valid amount")
                    return
                run_action(self, lambda: shifts.open_shift(self.db, value, branch_id(self.db)))
        self.ctx.notify()


# ---- dialogs -------------------------------------------------------------------


class CheckoutDialog(QDialog):
    def __init__(self, ctx: AppContext, cart: sales.Cart, totals: sales.CartTotals,
                 parent: QWidget, approved_by: int | None = None) -> None:
        super().__init__(parent)
        self.ctx, self.cart, self.totals = ctx, cart, totals
        self.approved_by = approved_by
        self.sale_id: int | None = None
        self.setWindowTitle("Payment")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)

        layout.addWidget(muted("Amount due"))
        due = heading(ctx.money(totals.total), 28)
        layout.addWidget(due)

        form = QFormLayout()
        self.cash = MoneyEdit(ctx, totals.total)
        self.card = MoneyEdit(ctx)
        self.bank = MoneyEdit(ctx)
        self.account = MoneyEdit(ctx)
        self.account.setEnabled(cart.customer_id is not None)
        if cart.customer_id is None:
            self.account.setPlaceholderText("Select a customer first")
        form.addRow("Cash received", self.cash)
        form.addRow("Card", self.card)
        form.addRow("Bank transfer", self.bank)
        form.addRow("On account", self.account)
        layout.addLayout(form)

        quick = QHBoxLayout()
        for text, method in (("Exact cash", "cash"), ("All card", "card"),
                             ("All bank", "bank"), ("All on account", "account")):
            b = button(text, lambda _=False, m=method: self._all(m))
            if method == "account":
                b.setEnabled(cart.customer_id is not None)
            quick.addWidget(b)
        layout.addLayout(quick)

        self.cash_notes = QHBoxLayout()
        for note in self._round_amounts(totals.total):
            self.cash_notes.addWidget(button(ctx.money(note), lambda _=False, n=note: self._cash(n)))
        self.cash_notes.addStretch(1)
        layout.addLayout(self.cash_notes)

        self.status = heading("", 14)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.complete = buttons.addButton("Complete sale", QDialogButtonBox.AcceptRole)
        self.complete.setObjectName("primary")
        self.complete.setMinimumHeight(40)
        buttons.accepted.connect(self._complete)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for edit in (self.cash, self.card, self.bank, self.account):
            edit.textChanged.connect(self._update)
        self._update()
        self.cash.setFocus()
        self.cash.selectAll()

    def _round_amounts(self, total: int) -> list[int]:
        unit = 10**self.ctx.fmt.decimals * (1000 if self.ctx.fmt.decimals == 0 else 1)
        out: list[int] = []
        for note in (5, 10, 20, 50, 100):
            v = -(-total // (note * unit)) * note * unit
            if v > total and v not in out:
                out.append(v)
        return out[:4]

    def _all(self, method: str) -> None:
        for m, edit in (("cash", self.cash), ("card", self.card), ("bank", self.bank),
                        ("account", self.account)):
            edit.set_value(self.totals.total if m == method else 0)

    def _cash(self, amount: int) -> None:
        self.card.set_value(0)
        self.bank.set_value(0)
        self.account.set_value(0)
        self.cash.set_value(amount)

    def _values(self) -> dict[str, int | None]:
        return {"cash": self.cash.value(), "card": self.card.value(),
                "bank": self.bank.value(), "account": self.account.value()}

    def _update(self) -> None:
        v = self._values()
        m = self.ctx.money
        ok = False
        if any(x is None for x in v.values()):
            self.status.setText("Enter valid amounts")
        else:
            non_cash = v["card"] + v["bank"] + v["account"]
            cash_needed = self.totals.total - non_cash
            if cash_needed < 0:
                self.status.setText(f"Non-cash payments exceed the total by {m(-cash_needed)}")
            elif v["cash"] < cash_needed:
                self.status.setText(f"Remaining: {m(cash_needed - v['cash'])}")
            else:
                change = v["cash"] - cash_needed
                self.status.setText(f"Change due: {m(change)}" if change else "Paid in full")
                ok = True
        self.complete.setEnabled(ok)

    def _complete(self) -> None:
        v = self._values()
        non_cash = v["card"] + v["bank"] + v["account"]
        payments = {"cash": self.totals.total - non_cash, "card": v["card"],
                    "bank": v["bank"], "account": v["account"]}
        sale_id = run_action(self, lambda: sales.checkout(
            self.ctx.db, self.cart, payments, tendered_cash=v["cash"],
            approved_by=self.approved_by, branch_id=branch_id(self.ctx.db)))
        if sale_id:
            self.sale_id = sale_id
            self.accept()


class ReceiptDialog(QDialog):
    def __init__(self, ctx: AppContext, sale_id: int, parent: QWidget,
                 title: str = "Receipt") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(420, 620)
        sale = sales.get_sale(ctx.db, sale_id)
        self.html = receipt_html(ctx.db, sale)
        layout = QVBoxLayout(self)
        view = QTextBrowser()
        view.setHtml(self.html)
        layout.addWidget(view, 1)
        row = QHBoxLayout()
        name = f"receipt-{sale['receipt_no']:05d}"
        row.addWidget(button("Print…", lambda: print_direct(self, self.html)))
        row.addWidget(button("Preview…", lambda: print_preview(self, self.html)))
        row.addWidget(button("Save PDF…", lambda: save_pdf(self, self.html, f"{name}.pdf")))
        row.addStretch(1)
        done = button("New sale" if title == "Sale complete" else "Close", self.accept, primary=True)
        done.setDefault(True)
        row.addWidget(done)
        layout.addLayout(row)


class HeldOrdersDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.held_id: int | None = None
        self.setWindowTitle("Held orders")
        self.resize(560, 360)
        layout = QVBoxLayout(self)
        self.table = DataTable(["#", "Held at", "Label", "Items", "Total"], right_align=(3, 4),
                               stretch=2)
        self.table.activated_key.connect(self._recall)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Delete", self._delete))
        row.addStretch(1)
        row.addWidget(button("Cancel", self.reject))
        row.addWidget(button("Recall", lambda: self._recall(self.table.selected_key()), primary=True))
        layout.addLayout(row)
        self._load()

    def _load(self) -> None:
        rate = st.get_int(self.ctx.db, "tax_rate_bps")
        held = sales.list_held(self.ctx.db, branch_id(self.ctx.db))
        rows = []
        for h in held:
            total = sales.price_cart(h["cart"], rate).total
            rows.append([(str(h["id"]), h["id"]), h["created_at"], h["label"] or "—",
                         (str(h["items"]), h["items"]), (self.ctx.money(total), total)])
        self.table.set_rows(rows, [h["id"] for h in held])
        if held:
            self.table.selectRow(0)

    def _recall(self, key) -> None:
        if key is not None:
            self.held_id = key
            self.accept()

    def _delete(self) -> None:
        key = self.table.selected_key()
        if key is not None and confirm(self, "Delete this held order?"):
            sales.take_held(self.ctx.db, key, branch_id(self.ctx.db))
            self._load()
            self.ctx.notify()


class CloseShiftDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        shift = shifts.current_shift(ctx.db, branch_id(ctx.db))
        self.expected = shifts.expected_cash(ctx.db, shift)
        self.setWindowTitle(f"Close shift #{shift['id']}")
        form = QFormLayout(self)
        form.addRow("Opened", QLabel(shift["opened_at"]))
        form.addRow("Opening float", QLabel(ctx.money(shift["opening_float"])))
        form.addRow("Cash taken (net)", QLabel(ctx.money(self.expected - shift["opening_float"])))
        form.addRow("Expected in drawer", heading(ctx.money(self.expected), 13))
        self.counted = MoneyEdit(ctx, self.expected)
        self.counted.textChanged.connect(self._update)
        form.addRow("Counted cash", self.counted)
        self.diff = QLabel()
        form.addRow("Difference", self.diff)
        self.notes = QLineEdit()
        form.addRow("Notes", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok = buttons.addButton("Close shift", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self._close)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._update()

    def _update(self) -> None:
        v = self.counted.value()
        self.ok.setEnabled(v is not None)
        if v is not None:
            d = v - self.expected
            self.diff.setText("Balanced" if d == 0 else
                              f"{'Over' if d > 0 else 'Short'} by {self.ctx.money(abs(d))}")

    def _close(self) -> None:
        result = run_action(self, lambda: shifts.close_shift(
            self.ctx.db, self.counted.value(), self.notes.text().strip(), branch_id(self.ctx.db)))
        if result:
            info(self, f"Shift closed.\nExpected {self.ctx.money(result['expected_cash'])}, "
                       f"counted {self.ctx.money(result['counted_cash'])}.")
            self.accept()
