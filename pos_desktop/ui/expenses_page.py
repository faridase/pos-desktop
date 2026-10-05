"""Operating expenses."""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QWidget,
)

from ..core import accounting as acc
from ..core import expenses, parties
from .common import (
    AppContext, DataTable, DateRangeBar, MoneyEdit, Page, account_combo, button, confirm,
    date_edit, error, export_buttons, iso, muted, page_layout, run_action,
)
from .inventory_page import pay_from_accounts


class ExpensesPage(Page):
    title = "Expenses"
    permission = "expenses"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        layout = page_layout(self, "Expenses")
        bar = QHBoxLayout()
        self.range = DateRangeBar("This month")
        self.range.changed.connect(self.refresh)
        bar.addWidget(self.range)
        bar.addStretch(1)
        bar.addWidget(button("New expense…", self._add, primary=True))
        bar.addWidget(button("Void", self._void))
        layout.addLayout(bar)
        self.table = DataTable(["Date", "Payee", "Expense account", "Paid from", "Amount", "Memo",
                                "Status"], right_align=(4,), stretch=5)
        layout.addWidget(self.table, 1)
        self.total = muted()
        layout.addWidget(self.total)
        layout.addLayout(export_buttons(self, self.table, lambda: "Expenses", self.range.label))

    def refresh(self) -> None:
        start, end = self.range.range()
        rows = expenses.list_expenses(self.db, start, end)
        m = self.ctx.money
        colors = {n: QColor("gray") for n, r in enumerate(rows) if r["voided"]}
        self.table.set_rows([
            [r["date"], r["payee"], r["account"], r["paid_from"], (m(r["amount"]), r["amount"]),
             r["memo"], "Voided" if r["voided"] else ""] for r in rows],
            [r["id"] for r in rows], colors=colors)
        total = sum(r["amount"] for r in rows if not r["voided"])
        self.total.setText(f"{sum(1 for r in rows if not r['voided'])} expenses · total {m(total)}")

    def _add(self) -> None:
        if ExpenseDialog(self.ctx, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _void(self) -> None:
        eid = self.table.selected_key()
        if eid and confirm(self, "Void this expense? A reversing journal entry will be posted."):
            run_action(self, lambda: expenses.void_expense(self.db, eid))
            self.ctx.notify()


class ExpenseDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("New expense")
        self.setMinimumWidth(440)
        form = QFormLayout(self)
        self.date = date_edit()
        self.account = account_combo(acc.list_accounts(ctx.db, types=("expense",)))
        # System accounts like COGS are posted automatically; hide them here.
        for key in ("cogs", "shrinkage", "over_short"):
            i = self.account.findData(acc.account_id(ctx.db, key))
            if i >= 0:
                self.account.removeItem(i)
        self.paid_from = account_combo(pay_from_accounts(ctx.db), acc.account_id(ctx.db, "bank"))
        self.supplier = QComboBox()
        self.supplier.addItem("— None —", None)
        for s in parties.list_parties(ctx.db, "suppliers"):
            self.supplier.addItem(s.name, s.id)
        self.payee = QLineEdit()
        self.amount = MoneyEdit(ctx)
        self.memo = QLineEdit()
        form.addRow("Date", self.date)
        form.addRow("Expense account", self.account)
        form.addRow("Amount", self.amount)
        form.addRow("Paid from", self.paid_from)
        form.addRow("Supplier", self.supplier)
        form.addRow("Payee", self.payee)
        form.addRow("Memo", self.memo)
        form.addRow("", muted("Choose Accounts Payable under “Paid from” to record a bill "
                              "you'll pay later; a supplier is then required."))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        amount = self.amount.value()
        if not amount:
            error(self, "Enter an amount")
            return
        if run_action(self, lambda: expenses.record_expense(
                self.ctx.db, self.account.currentData(), self.paid_from.currentData(), amount,
                self.payee.text().strip(), self.memo.text().strip(), self.supplier.currentData(),
                iso(self.date))):
            self.accept()
