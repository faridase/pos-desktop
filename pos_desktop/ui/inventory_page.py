"""Stock levels, purchasing and stock adjustments."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QSpinBox, QSplitter, QTableWidget,
    QTabWidget, QVBoxLayout, QWidget,
)

from ..core import accounting as acc
from ..core import catalog, parties
from ..core.catalog import PurchaseLine
from .common import (
    AppContext, DataTable, DateRangeBar, MoneyEdit, Page, button, date_edit, error,
    export_buttons, heading, iso, muted, page_layout, run_action,
)

REASONS = {"opening": "Opening", "purchase": "Purchase", "sale": "Sale", "refund": "Refund",
           "adjustment": "Adjustment"}


def product_combo(db, allow_untracked: bool = True) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    for p in catalog.list_products(db):
        if allow_untracked or p.track_stock:
            combo.addItem(f"{p.name}  [{p.sku}]", p.id)
    combo.completer().setFilterMode(Qt.MatchContains)
    combo.completer().setCompletionMode(QCompleter.PopupCompletion)
    combo.setCurrentIndex(-1)
    return combo


def pay_from_accounts(db) -> list[acc.Account]:
    return acc.money_accounts(db) + [acc.get_account(db, acc.account_id(db, "ap"))]


class InventoryPage(Page):
    title = "Inventory"
    permission = "inventory"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        layout = page_layout(self, "Inventory")
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # Stock levels
        stock = QWidget()
        sv = QVBoxLayout(stock)
        bar = QHBoxLayout()
        self.low_only = QCheckBox("Low stock only")
        self.low_only.toggled.connect(self.refresh)
        bar.addWidget(self.low_only)
        bar.addStretch(1)
        bar.addWidget(button("Receive stock / purchase…", self._purchase, primary=True))
        bar.addWidget(button("Stock count / adjust…", self._adjust))
        sv.addLayout(bar)
        self.stock_table = DataTable(["SKU", "Product", "Category", "On hand", "Reorder at",
                                      "Unit cost", "Stock value", "Retail value"],
                                     right_align=(3, 4, 5, 6, 7), stretch=1)
        self.stock_table.activated_key.connect(lambda pid: self._adjust(pid))
        sv.addWidget(self.stock_table, 1)
        self.stock_total = muted()
        sv.addWidget(self.stock_total)
        sv.addLayout(export_buttons(self, self.stock_table, lambda: "Inventory valuation",
                                    lambda: "Stock on hand"))
        self.tabs.addTab(stock, "Stock levels")

        # Purchases
        purchases = QWidget()
        pv = QVBoxLayout(purchases)
        bar = QHBoxLayout()
        self.p_range = DateRangeBar("This month")
        self.p_range.changed.connect(self.refresh)
        bar.addWidget(self.p_range)
        bar.addStretch(1)
        bar.addWidget(button("New purchase…", self._purchase, primary=True))
        pv.addLayout(bar)
        split = QSplitter(Qt.Vertical)
        self.p_table = DataTable(["#", "Date", "Supplier", "Reference", "Items", "Paid from",
                                  "Total"], right_align=(0, 4, 6), stretch=2)
        self.p_table.itemSelectionChanged.connect(self._show_purchase)
        self.p_items = DataTable(["SKU", "Product", "Qty", "Unit cost", "Line total"],
                                 right_align=(2, 3, 4), stretch=1)
        split.addWidget(self.p_table)
        split.addWidget(self.p_items)
        pv.addWidget(split, 1)
        self.tabs.addTab(purchases, "Purchases")

        # Movements
        moves = QWidget()
        mv = QVBoxLayout(moves)
        self.m_range = DateRangeBar("Last 7 days")
        self.m_range.changed.connect(self.refresh)
        mv.addWidget(self.m_range)
        self.m_table = DataTable(["Date & time", "SKU", "Product", "Change", "Unit cost",
                                  "Reason", "Reference", "Staff"], right_align=(3, 4), stretch=6)
        mv.addWidget(self.m_table, 1)
        mv.addLayout(export_buttons(self, self.m_table, lambda: "Stock movements",
                                    self.m_range.label))
        self.tabs.addTab(moves, "Movements")

    def refresh(self) -> None:
        m = self.ctx.money
        rows = catalog.inventory_valuation(self.db)
        if self.low_only.isChecked():
            rows = [r for r in rows if r["stock"] <= r["reorder_level"]]
        colors = {n: QColor("#c0392b") for n, r in enumerate(rows) if r["stock"] <= r["reorder_level"]}
        self.stock_table.set_rows([
            [r["sku"], r["name"], r["category"], (str(r["stock"]), r["stock"]),
             (str(r["reorder_level"]), r["reorder_level"]), (m(r["cost"]), r["cost"]),
             (m(r["value"]), r["value"]), (m(r["retail_value"]), r["retail_value"])]
            for r in rows], [r["id"] for r in rows], colors=colors)
        ledger = acc.account_balance(self.db, acc.account_id(self.db, "inventory"))
        self.stock_total.setText(
            f"Stock value {m(sum(r['value'] for r in rows))} · retail value "
            f"{m(sum(r['retail_value'] for r in rows))} · Inventory account balance {m(ledger)}")

        start, end = self.p_range.range()
        purchases = catalog.list_purchases(self.db, start, end)
        self.p_table.set_rows([
            [(str(p["id"]), p["id"]), p["date"], p["supplier"] or "", p["reference"],
             (str(p["item_count"]), p["item_count"]), p["paid_from"], (m(p["total"]), p["total"])]
            for p in purchases], [p["id"] for p in purchases])
        self._show_purchase()

        start, end = self.m_range.range()
        moves = catalog.stock_movements(self.db, start, end)
        self.m_table.set_rows([
            [r["created_at"], r["sku"], r["name"], (f"{r['quantity']:+d}", r["quantity"]),
             (m(r["unit_cost"]), r["unit_cost"]), REASONS.get(r["reason"], r["reason"]),
             r["reference"], r["user_name"] or ""] for r in moves])

    def _show_purchase(self) -> None:
        pid = self.p_table.selected_key()
        items = catalog.purchase_items(self.db, pid) if pid else []
        m = self.ctx.money
        self.p_items.set_rows([
            [i["sku"], i["name"], (str(i["quantity"]), i["quantity"]),
             (m(i["unit_cost"]), i["unit_cost"]),
             (m(i["unit_cost"] * i["quantity"]), i["unit_cost"] * i["quantity"])] for i in items])

    def _purchase(self) -> None:
        if PurchaseDialog(self.ctx, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _adjust(self, product_id: int | None = None) -> None:
        pid = product_id if isinstance(product_id, int) else self.stock_table.selected_key()
        if AdjustDialog(self.ctx, pid, self).exec() == QDialog.Accepted:
            self.ctx.notify()


class PurchaseDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Receive stock")
        self.resize(760, 480)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.supplier = QComboBox()
        self.supplier.addItem("— No supplier —", None)
        for s in parties.list_parties(ctx.db, "suppliers"):
            self.supplier.addItem(s.name, s.id)
        self.pay_from = QComboBox()
        for a in pay_from_accounts(ctx.db):
            label = a.label + ("  (buy on credit)" if a.system_key == "ap" else "")
            self.pay_from.addItem(label, a.id)
        self.date = date_edit()
        self.reference = QLineEdit()
        self.reference.setPlaceholderText("Supplier invoice number")
        form.addRow("Supplier", self.supplier)
        form.addRow("Paid from", self.pay_from)
        form.addRow("Date", self.date)
        form.addRow("Reference", self.reference)
        layout.addLayout(form)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Product", "Quantity", "Unit cost", "Line total"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Add line", self._add_line))
        row.addWidget(button("Remove line", self._remove_line))
        row.addStretch(1)
        self.total = heading("", 14)
        row.addWidget(self.total)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.addButton("Record purchase", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._add_line()

    def _add_line(self) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        combo = product_combo(self.ctx.db, allow_untracked=False)
        qty = QSpinBox()
        qty.setRange(1, 1_000_000)
        cost = MoneyEdit(self.ctx)
        total = QLabel()
        total.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        def product_chosen() -> None:
            pid = combo.currentData()
            if pid:
                cost.set_value(catalog.get_product(self.ctx.db, pid).cost)
        combo.currentIndexChanged.connect(product_chosen)
        qty.valueChanged.connect(self._update)
        cost.textChanged.connect(self._update)
        self.table.setCellWidget(r, 0, combo)
        self.table.setCellWidget(r, 1, qty)
        self.table.setCellWidget(r, 2, cost)
        self.table.setCellWidget(r, 3, total)
        combo.setFocus()
        self._update()

    def _remove_line(self) -> None:
        r = self.table.currentRow()
        self.table.removeRow(r if r >= 0 else self.table.rowCount() - 1)
        self._update()

    def _lines(self) -> list[tuple[int | None, int, int | None]]:
        out = []
        for r in range(self.table.rowCount()):
            out.append((self.table.cellWidget(r, 0).currentData(),
                        self.table.cellWidget(r, 1).value(),
                        self.table.cellWidget(r, 2).value()))
        return out

    def _update(self) -> None:
        total = 0
        for r, (_, qty, cost) in enumerate(self._lines()):
            line = qty * (cost or 0)
            total += line
            self.table.cellWidget(r, 3).setText(self.ctx.money(line))
        self.total.setText(f"Total {self.ctx.money(total)}")

    def _save(self) -> None:
        lines = []
        for pid, qty, cost in self._lines():
            if pid is None:
                continue
            if cost is None:
                error(self, "Enter valid unit costs")
                return
            lines.append(PurchaseLine(pid, qty, cost))
        ok = run_action(self, lambda: catalog.record_purchase(
            self.ctx.db, lines, self.pay_from.currentData(), self.supplier.currentData(),
            self.reference.text().strip(), iso(self.date)))
        if ok:
            self.accept()


class AdjustDialog(QDialog):
    def __init__(self, ctx: AppContext, product_id: int | None, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Stock count / adjustment")
        form = QFormLayout(self)
        self.product = product_combo(ctx.db, allow_untracked=False)
        if product_id:
            self.product.setCurrentIndex(self.product.findData(product_id))
        self.current = QLabel("–")
        self.counted = QSpinBox()
        self.counted.setRange(-1_000_000, 1_000_000)
        self.change = QLabel()
        self.reason = QComboBox()
        self.reason.setEditable(True)
        self.reason.addItems(["Stock count", "Damaged", "Expired", "Theft / loss", "Found stock",
                              "Other"])
        form.addRow("Product", self.product)
        form.addRow("Currently recorded", self.current)
        form.addRow("Actual quantity", self.counted)
        form.addRow("Change", self.change)
        form.addRow("Reason", self.reason)
        form.addRow("", muted("The value of the difference (at unit cost) is recorded "
                              "as Inventory Shrinkage."))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.product.currentIndexChanged.connect(self._load)
        self.counted.valueChanged.connect(self._update)
        self._load()

    def _load(self) -> None:
        pid = self.product.currentData()
        if pid:
            p = catalog.get_product(self.ctx.db, pid)
            self.current.setText(str(p.stock))
            self.counted.setValue(p.stock)
            self._stock, self._cost = p.stock, p.cost
        else:
            self._stock, self._cost = 0, 0
        self._update()

    def _update(self) -> None:
        d = self.counted.value() - self._stock
        self.change.setText(f"{d:+d}  ({self.ctx.money(d * self._cost)})" if d else "No change")

    def _save(self) -> None:
        pid = self.product.currentData()
        if not pid:
            error(self, "Choose a product")
            return
        if run_action(self, lambda: catalog.adjust_stock(
                self.ctx.db, pid, self.counted.value(), self.reason.currentText()) is not None):
            self.accept()
