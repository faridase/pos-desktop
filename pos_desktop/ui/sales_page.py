"""Sales history, receipts and refunds."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLineEdit, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from ..core import sales
from ..core.money import round_div
from ..core.receipts import receipt_html
from .auth_dialogs import request_permission
from .common import (
    RIGHT, AppContext, DataTable, DateRangeBar, Page, button, error, heading, muted,
    page_layout, print_direct, run_action, save_pdf,
)

STATUS = {"completed": "Completed", "refunded": "Refunded",
          "partially_refunded": "Part refunded"}


class SalesPage(Page):
    title = "Sales"
    permission = "view_sales"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        layout = page_layout(self, "Sales history")
        bar = QHBoxLayout()
        self.range = DateRangeBar("Last 7 days")
        self.range.changed.connect(self.refresh)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Receipt #, customer or note")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.refresh)
        bar.addWidget(self.range)
        bar.addWidget(self.search, 1)
        bar.addWidget(button("Search", self.refresh))
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable(["Receipt", "Date & time", "Staff", "Branch", "Customer", "Items",
                                "Payment", "Total", "Refunded", "Status"],
                               right_align=(0, 5, 7, 8), stretch=4)
        self.table.itemSelectionChanged.connect(self._show)
        self.summary = muted()
        lv.addWidget(self.table, 1)
        lv.addWidget(self.summary)
        splitter.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.preview = QTextBrowser()
        rv.addWidget(self.preview, 1)
        row = QHBoxLayout()
        self.print_btn = button("Print…", lambda: print_direct(self, self._html))
        self.pdf_btn = button("Save PDF…", lambda: save_pdf(self, self._html, "receipt.pdf"))
        self.refund_btn = button("Refund…", self._refund)
        for b in (self.print_btn, self.pdf_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.refund_btn)
        rv.addLayout(row)
        splitter.addWidget(right)
        splitter.setSizes([760, 380])
        layout.addWidget(splitter, 1)
        self._html = ""
        self._show()

    def refresh(self) -> None:
        start, end = self.range.range()
        rows = sales.list_sales(self.db, start, end, self.search.text().strip())
        m = self.ctx.money
        self.table.set_rows([
            [(f"#{r['receipt_no']:05d}", r["receipt_no"]), r["created_at"], r["cashier"] or "",
             r["branch"] or "", r["customer"] or "",
             (str(r["item_count"] or 0), r["item_count"] or 0), r["methods"] or "",
             (m(r["total"]), r["total"]), (m(r["refunded"]) if r["refunded"] else "", r["refunded"]),
             STATUS.get(r["status"], r["status"])]
            for r in rows
        ], [r["id"] for r in rows])
        total = sum(r["total"] for r in rows)
        refunded = sum(r["refunded"] for r in rows)
        self.summary.setText(f"{len(rows)} sales · {m(total)} total · {m(refunded)} refunded")
        self._show()

    def _show(self) -> None:
        sale_id = self.table.selected_key()
        enabled = sale_id is not None
        for b in (self.print_btn, self.pdf_btn, self.refund_btn):
            b.setEnabled(enabled)
        if not enabled:
            self.preview.setHtml("<p style='color:gray'>Select a sale to see its receipt.</p>")
            self._html = ""
            return
        sale = sales.get_sale(self.db, sale_id)
        self._html = receipt_html(self.db, sale)
        self.preview.setHtml(self._html)
        self.refund_btn.setEnabled(sale["status"] != "refunded")

    def _refund(self) -> None:
        sale_id = self.table.selected_key()
        if not sale_id:
            return
        receipt = sales.get_sale(self.db, sale_id)["receipt_no"]
        approver = request_permission(self, self.ctx, "refund", f"Refund on sale #{receipt:05d}")
        if approver is None:
            return
        approved_by = approver if approver != self.ctx.user.id else None
        if RefundDialog(self.ctx, sale_id, self, approved_by).exec() == QDialog.Accepted:
            self.ctx.notify()


class RefundDialog(QDialog):
    def __init__(self, ctx: AppContext, sale_id: int, parent: QWidget,
                 approved_by: int | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.approved_by = approved_by
        self.sale = sales.get_sale(ctx.db, sale_id)
        self.setWindowTitle(f"Refund sale #{self.sale['receipt_no']:05d}")
        self.resize(640, 440)
        layout = QVBoxLayout(self)
        layout.addWidget(muted("Choose how many of each item are being returned."))

        items = [i for i in self.sale["items"] if i["quantity"] > i["refunded_qty"]]
        self.items = items
        self.table = QTableWidget(len(items), 5)
        self.table.setHorizontalHeaderLabels(["Item", "Sold", "Already refunded", "Return", "Paid"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.spins: list[QSpinBox] = []
        for r, i in enumerate(items):
            for c, text in enumerate([i["name"], str(i["quantity"]), str(i["refunded_qty"]), "",
                                      ctx.money(i["net"] + i["tax"])]):
                cell = QTableWidgetItem(text)
                cell.setFlags(Qt.ItemIsEnabled)
                if c:
                    cell.setTextAlignment(RIGHT)
                self.table.setItem(r, c, cell)
            spin = QSpinBox()
            spin.setRange(0, i["quantity"] - i["refunded_qty"])
            spin.setValue(spin.maximum())
            spin.valueChanged.connect(self._update)
            self.table.setCellWidget(r, 3, spin)
            self.spins.append(spin)
        layout.addWidget(self.table, 1)

        form = QFormLayout()
        self.method = QComboBox()
        for key, label in sales.PAYMENT_METHODS.items():
            if key == "account" and not self.sale["customer_id"]:
                continue
            self.method.addItem(label, key)
        if self.sale["payments"]:
            self.method.setCurrentIndex(max(0, self.method.findData(self.sale["payments"][0]["method"])))
        self.restock = QCheckBox("Return items to stock")
        self.restock.setChecked(True)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("e.g. damaged, wrong item")
        form.addRow("Refund via", self.method)
        form.addRow("", self.restock)
        form.addRow("Reason", self.reason)
        self.total = heading("", 16)
        form.addRow("Refund amount", self.total)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok = buttons.addButton("Refund", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update()

    def _update(self) -> None:
        total = 0
        for i, spin in zip(self.items, self.spins):
            q = spin.value()
            if q == i["quantity"] - i["refunded_qty"]:
                total += i["net"] - i["refunded_net"] + i["tax"] - i["refunded_tax"]
            elif q:
                total += round_div(i["net"] * q, i["quantity"]) + round_div(i["tax"] * q, i["quantity"])
        self.total.setText(self.ctx.money(total))
        self.ok.setEnabled(total > 0 or any(s.value() for s in self.spins))

    def _save(self) -> None:
        quantities = {i["id"]: s.value() for i, s in zip(self.items, self.spins) if s.value()}
        if not quantities:
            error(self, "Choose at least one item")
            return
        if run_action(self, lambda: sales.refund(
                self.ctx.db, self.sale["id"], quantities, self.method.currentData(),
                self.restock.isChecked(), self.reason.text().strip(),
                approved_by=self.approved_by)):
            self.accept()
