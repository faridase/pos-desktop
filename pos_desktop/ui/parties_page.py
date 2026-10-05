"""Customers and suppliers with their account balances."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QSplitter, QVBoxLayout, QWidget,
)

from ..core import accounting as acc
from ..core import parties
from ..core.parties import Party
from .common import (
    AppContext, DataTable, MoneyEdit, Page, button, date_edit, error, export_buttons,
    heading, iso, page_layout, run_action,
)


class PartiesPage(Page):
    def __init__(self, ctx: AppContext, kind: str) -> None:
        super().__init__(ctx)
        self.kind = kind
        self.is_customer = kind == "customers"
        self.title = "Customers" if self.is_customer else "Suppliers"
        self.permission = kind
        layout = page_layout(
            self, self.title,
            "Balance is what the customer owes you (sales on account)." if self.is_customer
            else "Balance is what you owe the supplier (purchases and expenses on credit).")
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, phone or email")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.inactive = QCheckBox("Show inactive")
        self.inactive.toggled.connect(self.refresh)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.inactive)
        layout.addLayout(bar)

        split = QSplitter(Qt.Vertical)
        self.table = DataTable(["Name", "Phone", "Email", "Balance", "Status"],
                               right_align=(3,), stretch=0)
        self.table.activated_key.connect(lambda _: self._edit())
        self.table.itemSelectionChanged.connect(self._show_statement)
        split.addWidget(self.table)
        box = QWidget()
        bv = QVBoxLayout(box)
        bv.setContentsMargins(0, 6, 0, 0)
        self.statement_title = heading("Statement", 12)
        bv.addWidget(self.statement_title)
        self.statement = DataTable(["Date", "Description", "Charges", "Payments", "Balance"],
                                   right_align=(2, 3, 4), stretch=1, sortable=False)
        bv.addWidget(self.statement, 1)
        bv.addLayout(export_buttons(self, self.statement, self._statement_name,
                                    lambda: "Account statement"))
        split.addWidget(box)
        split.setSizes([320, 260])
        layout.addWidget(split, 1)

        row = QHBoxLayout()
        row.addWidget(button(f"Add {'customer' if self.is_customer else 'supplier'}…",
                             self._add, primary=True))
        row.addWidget(button("Edit…", self._edit))
        row.addStretch(1)
        row.addWidget(button("Receive payment…" if self.is_customer else "Pay supplier…",
                             self._payment))
        layout.addLayout(row)

    def _balance(self, pid: int) -> int:
        return (parties.customer_balance if self.is_customer else parties.supplier_balance)(self.db, pid)

    def refresh(self) -> None:
        items = parties.list_parties(self.db, self.kind, self.search.text().strip(),
                                     self.inactive.isChecked())
        m = self.ctx.money
        rows = []
        for p in items:
            b = self._balance(p.id)
            rows.append([p.name, p.phone, p.email, (m(b), b), "Active" if p.is_active else "Inactive"])
        self.table.set_rows(rows, [p.id for p in items])
        self._show_statement()

    def _statement_name(self) -> str:
        pid = self.table.selected_key()
        name = parties.get_party(self.db, self.kind, pid).name if pid else ""
        return f"Statement - {name}"

    def _show_statement(self) -> None:
        pid = self.table.selected_key()
        m = self.ctx.money
        if not pid:
            self.statement_title.setText("Statement")
            self.statement.set_rows([])
            return
        self.statement_title.setText(f"Statement – {parties.get_party(self.db, self.kind, pid).name}")
        rows = (parties.customer_statement if self.is_customer
                else parties.supplier_statement)(self.db, pid)
        self.statement.set_rows([
            [r["date"], r["description"], m(r["charge"]) if r["charge"] else "",
             m(r["payment"]) if r["payment"] else "", m(r["balance"])] for r in rows])

    def _add(self) -> None:
        if PartyDialog(self.ctx, self.kind, None, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _edit(self) -> None:
        pid = self.table.selected_key()
        if pid and PartyDialog(self.ctx, self.kind, parties.get_party(self.db, self.kind, pid),
                               self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _payment(self) -> None:
        pid = self.table.selected_key()
        if not pid:
            error(self, f"Select a {'customer' if self.is_customer else 'supplier'} first")
            return
        if PaymentDialog(self.ctx, self.kind, pid, self._balance(pid), self).exec():
            self.ctx.notify()


class PartyDialog(QDialog):
    def __init__(self, ctx: AppContext, kind: str, party: Party | None, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx, self.kind = ctx, kind
        self.party = party or Party()
        noun = "customer" if kind == "customers" else "supplier"
        self.setWindowTitle(f"Edit {noun}" if party else f"Add {noun}")
        self.setMinimumWidth(400)
        form = QFormLayout(self)
        p = self.party
        self.name, self.phone = QLineEdit(p.name), QLineEdit(p.phone)
        self.email, self.address = QLineEdit(p.email), QLineEdit(p.address)
        self.notes = QPlainTextEdit(p.notes)
        self.notes.setFixedHeight(70)
        self.active = QCheckBox("Active")
        self.active.setChecked(p.is_active)
        for label, w in (("Name", self.name), ("Phone", self.phone), ("Email", self.email),
                         ("Address", self.address), ("Notes", self.notes), ("", self.active)):
            form.addRow(label, w)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        p = self.party
        p.name, p.phone, p.email = self.name.text(), self.phone.text(), self.email.text()
        p.address, p.notes = self.address.text(), self.notes.toPlainText()
        p.is_active = self.active.isChecked()
        if run_action(self, lambda: parties.save_party(self.ctx.db, self.kind, p)):
            self.accept()


class PaymentDialog(QDialog):
    def __init__(self, ctx: AppContext, kind: str, party_id: int, balance: int,
                 parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx, self.kind, self.party_id = ctx, kind, party_id
        is_customer = kind == "customers"
        party = parties.get_party(ctx.db, kind, party_id)
        self.setWindowTitle(("Payment from " if is_customer else "Payment to ") + party.name)
        form = QFormLayout(self)
        form.addRow("Current balance", QLabel(ctx.money(balance)))
        self.amount = MoneyEdit(ctx, max(balance, 0))
        self.method = QComboBox()
        if is_customer:
            for key, label in (("cash", "Cash"), ("card", "Card"), ("bank", "Bank transfer")):
                self.method.addItem(label, key)
        else:
            for a in acc.money_accounts(ctx.db):
                self.method.addItem(a.label, a.id)
            self.method.setCurrentIndex(max(0, self.method.findData(acc.account_id(ctx.db, "bank"))))
        self.date = date_edit()
        self.memo = QLineEdit()
        form.addRow("Amount", self.amount)
        form.addRow("Received as" if is_customer else "Paid from", self.method)
        form.addRow("Date", self.date)
        form.addRow("Memo", self.memo)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        amount = self.amount.value()
        if not amount:
            error(self, "Enter an amount")
            return
        fn = parties.receive_customer_payment if self.kind == "customers" else parties.pay_supplier
        if run_action(self, lambda: fn(self.ctx.db, self.party_id, amount, self.method.currentData(),
                                       self.memo.text().strip(), iso(self.date))):
            self.accept()
