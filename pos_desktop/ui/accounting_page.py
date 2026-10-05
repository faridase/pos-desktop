"""Chart of accounts, journal, general ledger and cash drawer shifts."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QTableWidget, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core import accounting as acc
from ..core import shifts
from ..core.accounting import Line
from .common import (
    RIGHT, AppContext, DataTable, DateRangeBar, MoneyEdit, Page, account_combo, button,
    confirm, date_edit, error, export_buttons, heading, iso, muted, page_layout, run_action,
)

SOURCES = {
    "": "All sources", "sale": "Sales", "refund": "Refunds", "purchase": "Purchases",
    "expense": "Expenses", "customer_payment": "Customer payments",
    "supplier_payment": "Supplier payments", "adjustment": "Stock adjustments",
    "opening": "Opening balances", "shift": "Cash over/short", "transfer": "Transfers",
    "manual": "Manual entries", "reversal": "Reversals",
}


class AccountingPage(Page):
    title = "Accounting"
    permission = "accounting"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        layout = page_layout(self, "Accounting",
                             "Double-entry books. Sales, purchases, expenses and payments "
                             "post here automatically.")
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_chart()
        self._build_journal()
        self._build_ledger()
        self._build_shifts()

    # ---- chart of accounts --------------------------------------------------------

    def _build_chart(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.show_inactive = QCheckBox("Show inactive")
        self.show_inactive.toggled.connect(self.refresh)
        bar.addWidget(self.show_inactive)
        bar.addStretch(1)
        bar.addWidget(button("Add account…", self._add_account, primary=True))
        bar.addWidget(button("Edit…", self._edit_account))
        bar.addWidget(button("View ledger", self._open_ledger))
        v.addLayout(bar)
        self.chart = DataTable(["Code", "Name", "Type", "Balance", "System", "Status"],
                               right_align=(3,), stretch=1)
        self.chart.activated_key.connect(lambda _: self._open_ledger())
        v.addWidget(self.chart, 1)
        v.addWidget(muted("System accounts are used by automatic postings and can be renamed "
                          "but not removed."))
        self.tabs.addTab(w, "Chart of accounts")

    def _refresh_chart(self) -> None:
        bal = acc.balances(self.db)
        accounts = acc.list_accounts(self.db, include_inactive=self.show_inactive.isChecked())
        m = self.ctx.money
        self.chart.set_rows([
            [a.code, a.name, acc.TYPE_LABELS[a.type], (m(bal.get(a.id, 0)), bal.get(a.id, 0)),
             "Yes" if a.system_key else "", "Active" if a.is_active else "Inactive"]
            for a in accounts], [a.id for a in accounts],
            colors={n: QColor("gray") for n, a in enumerate(accounts) if not a.is_active})

    def _add_account(self) -> None:
        if AccountDialog(self.ctx, None, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _edit_account(self) -> None:
        aid = self.chart.selected_key()
        if aid and AccountDialog(self.ctx, acc.get_account(self.db, aid), self).exec():
            self.ctx.notify()

    def _open_ledger(self) -> None:
        aid = self.chart.selected_key()
        if aid:
            self.gl_account.setCurrentIndex(self.gl_account.findData(aid))
            self.tabs.setCurrentIndex(2)

    # ---- journal --------------------------------------------------------------------

    def _build_journal(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.j_range = DateRangeBar("This month")
        self.j_range.changed.connect(self.refresh)
        self.j_source = QComboBox()
        for key, label in SOURCES.items():
            self.j_source.addItem(label, key)
        self.j_source.currentIndexChanged.connect(self.refresh)
        self.j_search = QLineEdit()
        self.j_search.setPlaceholderText("Search memo")
        self.j_search.returnPressed.connect(self.refresh)
        bar.addWidget(self.j_range)
        bar.addWidget(self.j_source)
        bar.addWidget(self.j_search, 1)
        v.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Date", "#", "Description / account", "Debit", "Credit"])
        self.tree.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tree.header().setStretchLastSection(False)
        self.tree.setAlternatingRowColors(True)
        v.addWidget(self.tree, 1)
        row = QHBoxLayout()
        row.addWidget(button("New journal entry…", self._new_entry, primary=True))
        row.addWidget(button("Transfer funds…", self._transfer))
        row.addWidget(button("Reverse entry", self._reverse))
        row.addStretch(1)
        row.addWidget(button("Expand all", self.tree.expandAll))
        row.addWidget(button("Collapse all", self.tree.collapseAll))
        v.addLayout(row)
        self.tabs.addTab(w, "Journal")

    def _refresh_journal(self) -> None:
        start, end = self.j_range.range()
        entries = acc.journal(self.db, start, end, self.j_source.currentData() or None,
                              self.j_search.text().strip())
        m = self.ctx.money
        self.tree.clear()
        for e in entries:
            label = SOURCES.get(e["source"], e["source"])
            memo = e["memo"] + (f"   [reversed by #{e['reversed_by']}]" if e["reversed_by"] else "")
            if e["user_name"]:
                memo += f"  ·  by {e['user_name']}"
            top = QTreeWidgetItem([e["date"], str(e["id"]), f"{memo}  ·  {label}",
                                   m(e["amount"]), m(e["amount"])])
            top.setData(0, Qt.UserRole, e["id"])
            top.setData(1, Qt.UserRole, e["source"])
            f = top.font(0)
            f.setBold(True)
            for c in range(5):
                top.setFont(c, f)
            for c in (3, 4):
                top.setTextAlignment(c, RIGHT)
            for l in e["lines"]:
                account = f"{l['code']} · {l['name']}" + (f"  – {l['memo']}" if l["memo"] else "")
                child = QTreeWidgetItem(["", "", f"    {account}",
                                         m(l["debit"]) if l["debit"] else "",
                                         m(l["credit"]) if l["credit"] else ""])
                for c in (3, 4):
                    child.setTextAlignment(c, RIGHT)
                top.addChild(child)
            self.tree.addTopLevelItem(top)
        for c in (0, 1, 3, 4):
            self.tree.resizeColumnToContents(c)

    def _new_entry(self) -> None:
        if JournalEntryDialog(self.ctx, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _transfer(self) -> None:
        if TransferDialog(self.ctx, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _reverse(self) -> None:
        item = self.tree.currentItem()
        while item is not None and item.parent() is not None:
            item = item.parent()
        if item is None:
            return
        entry_id, source = item.data(0, Qt.UserRole), item.data(1, Qt.UserRole)
        if source not in ("manual", "transfer"):
            error(self, "Only manual entries and transfers can be reversed here. Use refunds, "
                        "voids or stock adjustments for automatic postings.")
            return
        if confirm(self, f"Post a reversing entry for #{entry_id}?"):
            run_action(self, lambda: acc.reverse(self.db, entry_id))
            self.ctx.notify()

    # ---- general ledger ---------------------------------------------------------------

    def _build_ledger(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.gl_account = QComboBox()
        self.gl_account.currentIndexChanged.connect(self._refresh_ledger)
        self.gl_range = DateRangeBar("This month")
        self.gl_range.changed.connect(self._refresh_ledger)
        bar.addWidget(QLabel("Account"))
        bar.addWidget(self.gl_account, 1)
        bar.addWidget(self.gl_range)
        v.addLayout(bar)
        self.gl = DataTable(["Date", "Entry", "Description", "Debit", "Credit", "Balance"],
                            right_align=(1, 3, 4, 5), stretch=2, sortable=False)
        v.addWidget(self.gl, 1)
        v.addLayout(export_buttons(self, self.gl, lambda: f"Ledger - {self.gl_account.currentText()}",
                                   self.gl_range.label))
        self.tabs.addTab(w, "General ledger")

    def _refresh_ledger(self) -> None:
        aid = self.gl_account.currentData()
        if aid is None:
            return
        start, end = self.gl_range.range()
        data = acc.general_ledger(self.db, aid, start, end)
        m = self.ctx.money
        rows = [["", "", "Opening balance", "", "", m(data["opening"])]]
        rows += [[r["date"], str(r["entry_id"]), r["memo"], m(r["debit"]) if r["debit"] else "",
                  m(r["credit"]) if r["credit"] else "", m(r["balance"])] for r in data["rows"]]
        rows.append(["", "", "Closing balance",
                     m(sum(r["debit"] for r in data["rows"])),
                     m(sum(r["credit"] for r in data["rows"])), m(data["closing"])])
        self.gl.set_rows(rows, bold_rows={0, len(rows) - 1})

    # ---- shifts -----------------------------------------------------------------------

    def _build_shifts(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(muted("Open and close shifts from the Register. Differences between "
                          "expected and counted cash are posted to Cash Over/Short."))
        self.shift_table = DataTable(["#", "Branch", "Opened", "By", "Closed", "By", "Float",
                                      "Expected", "Counted", "Difference", "Notes"],
                                     right_align=(0, 6, 7, 8, 9), stretch=10)
        v.addWidget(self.shift_table, 1)
        self.tabs.addTab(w, "Cash shifts")

    def _refresh_shifts(self) -> None:
        m = self.ctx.money
        rows = shifts.list_shifts(self.db)
        self.shift_table.set_rows([
            [(str(s["id"]), s["id"]), s["branch"] or "", s["opened_at"], s["opened_by_name"] or "",
             s["closed_at"] or "Open", s["closed_by_name"] or "", m(s["opening_float"]), m(s["expected_cash"]), m(s["counted_cash"]),
             m(s["difference"]), s["notes"]] for s in rows])

    # ---- refresh ------------------------------------------------------------------------

    def refresh(self) -> None:
        self._refresh_chart()
        current = self.gl_account.currentData()
        self.gl_account.blockSignals(True)
        self.gl_account.clear()
        for a in acc.list_accounts(self.db, include_inactive=True):
            self.gl_account.addItem(a.label, a.id)
        self.gl_account.setCurrentIndex(max(0, self.gl_account.findData(current)))
        self.gl_account.blockSignals(False)
        self._refresh_journal()
        self._refresh_ledger()
        self._refresh_shifts()


class AccountDialog(QDialog):
    def __init__(self, ctx: AppContext, account: acc.Account | None, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx, self.account = ctx, account
        self.setWindowTitle("Edit account" if account else "Add account")
        form = QFormLayout(self)
        self.code = QLineEdit(account.code if account else "")
        self.name = QLineEdit(account.name if account else "")
        self.type = QComboBox()
        for t in acc.ACCOUNT_TYPES:
            self.type.addItem(acc.TYPE_LABELS[t], t)
        if account:
            self.type.setCurrentIndex(self.type.findData(account.type))
            self.type.setEnabled(not account.system_key)
        self.active = QCheckBox("Active")
        self.active.setChecked(account.is_active if account else True)
        self.active.setEnabled(not (account and account.system_key))
        form.addRow("Code", self.code)
        form.addRow("Name", self.name)
        form.addRow("Type", self.type)
        form.addRow("", self.active)
        form.addRow("", muted("Assets: 1xxx · Liabilities: 2xxx · Equity: 3xxx · "
                              "Income: 4xxx · Expenses: 5xxx–6xxx"))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        if run_action(self, lambda: acc.save_account(
                self.ctx.db, self.code.text(), self.name.text(), self.type.currentData(),
                self.account.id if self.account else None, self.active.isChecked())):
            self.accept()


class JournalEntryDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.accounts = acc.list_accounts(ctx.db)
        self.setWindowTitle("New journal entry")
        self.resize(780, 460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.date = date_edit()
        self.memo = QLineEdit()
        self.memo.setPlaceholderText("e.g. Owner capital, tax remittance, loan repayment")
        form.addRow("Date", self.date)
        form.addRow("Description", self.memo)
        layout.addLayout(form)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Account", "Debit", "Credit", "Line memo"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Add line", self._add_line))
        row.addWidget(button("Remove line", lambda: (self.table.removeRow(self.table.currentRow()),
                                                     self._update())))
        row.addStretch(1)
        self.status = heading("", 12)
        row.addWidget(self.status)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok = buttons.addButton("Post entry", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._add_line()
        self._add_line()

    def _add_line(self) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        combo = account_combo(self.accounts)
        combo.setCurrentIndex(-1)
        debit, credit = MoneyEdit(self.ctx), MoneyEdit(self.ctx)
        debit.textChanged.connect(self._update)
        credit.textChanged.connect(self._update)
        self.table.setCellWidget(r, 0, combo)
        self.table.setCellWidget(r, 1, debit)
        self.table.setCellWidget(r, 2, credit)
        self.table.setCellWidget(r, 3, QLineEdit())
        self._update()

    def _lines(self) -> list[Line] | None:
        lines = []
        for r in range(self.table.rowCount()):
            aid = self.table.cellWidget(r, 0).currentData()
            d, c = self.table.cellWidget(r, 1).value(), self.table.cellWidget(r, 2).value()
            if d is None or c is None:
                return None
            if aid is None and (d or c):
                return None
            if aid is not None and (d or c):
                lines.append(Line(aid, d, c, self.table.cellWidget(r, 3).text().strip()))
        return lines

    def _update(self) -> None:
        lines = self._lines()
        if lines is None:
            self.status.setText("Choose an account and valid amounts for every line")
            self.ok.setEnabled(False)
            return
        d, c = sum(l.debit for l in lines), sum(l.credit for l in lines)
        m = self.ctx.money
        if d == c and d > 0:
            self.status.setText(f"Balanced · {m(d)}")
        else:
            self.status.setText(f"Debits {m(d)} · Credits {m(c)} · Difference {m(abs(d - c))}")
        self.ok.setEnabled(d == c and d > 0)

    def _save(self) -> None:
        lines = self._lines()
        if not self.memo.text().strip():
            error(self, "Enter a description")
            return
        if lines and run_action(self, lambda: acc.post(
                self.ctx.db, self.memo.text().strip(), lines, "manual", date=iso(self.date))):
            self.accept()


class TransferDialog(QDialog):
    """Moves money between accounts, e.g. depositing cash or settling card
    payments to the bank."""

    def __init__(self, ctx: AppContext, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Transfer funds")
        money = acc.money_accounts(ctx.db)
        form = QFormLayout(self)
        self.source = account_combo(money, acc.account_id(ctx.db, "card"))
        self.target = account_combo(money, acc.account_id(ctx.db, "bank"))
        self.amount = MoneyEdit(ctx)
        self.date = date_edit()
        self.memo = QLineEdit()
        self.memo.setPlaceholderText("e.g. Card settlement, cash deposit")
        form.addRow("From", self.source)
        form.addRow("To", self.target)
        form.addRow("Amount", self.amount)
        form.addRow("Date", self.date)
        form.addRow("Memo", self.memo)
        self.balance = muted()
        form.addRow("", self.balance)
        self.source.currentIndexChanged.connect(self._show_balance)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._show_balance()

    def _show_balance(self) -> None:
        b = acc.account_balance(self.ctx.db, self.source.currentData())
        self.balance.setText(f"Available in source account: {self.ctx.money(b)}")

    def _save(self) -> None:
        amount = self.amount.value()
        src, dst = self.source.currentData(), self.target.currentData()
        if not amount:
            error(self, "Enter an amount")
            return
        if src == dst:
            error(self, "Choose two different accounts")
            return
        memo = self.memo.text().strip() or "Transfer"
        if run_action(self, lambda: acc.post(self.ctx.db, memo, [
                Line(dst, debit=amount), Line(src, credit=amount)], "transfer",
                date=iso(self.date))):
            self.accept()
