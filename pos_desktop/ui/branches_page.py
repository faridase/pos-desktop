"""Branches and their order API keys (owner/admin)."""

from __future__ import annotations

from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from ..core import branches
from ..core.branches import Branch
from .common import (
    AppContext, DataTable, Page, button, confirm, error, heading, muted, page_layout, run_action,
)


class BranchesPage(Page):
    title = "Branches"
    permission = "branches"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        layout = page_layout(self, "Branches")
        self.subtitle = muted()
        layout.addWidget(self.subtitle)
        self.table = DataTable(["Code", "Name", "Address", "Phone", "Status", "Order API key"],
                               stretch=2)
        self.table.activated_key.connect(lambda _: self._edit())
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Add branch…", self._add, primary=True))
        row.addWidget(button("Edit…", self._edit))
        row.addWidget(button("Delete", self._delete))
        row.addStretch(1)
        self.key_button = button("Create API key…", self._generate)
        row.addWidget(self.key_button)
        row.addWidget(button("Revoke key", self._revoke))
        layout.addLayout(row)
        layout.addWidget(muted(
            "An online shop or ordering system sends delivery and pickup orders to a branch through "
            "the web app's order API, using that branch's API key. This app shares the data file, so "
            "those orders appear under Orders. For now every till runs as the head branch."))
        self.table.itemSelectionChanged.connect(self._selection_changed)

    def refresh(self) -> None:
        items = branches.list_branches(self.db)
        self.subtitle.setText(f"This POS runs as {branches.current_branch(self.db).name}.")
        self.table.set_rows([
            [b.code, b.name + ("  (head)" if b.is_head else ""), b.address, b.phone,
             "Active" if b.is_active else "Inactive",
             f"…{b.api_key_hint}" if b.has_api_key else "None"] for b in items],
            [b.id for b in items],
            colors={n: QColor("gray") for n, b in enumerate(items) if not b.is_active})
        self._selection_changed()

    def _selected(self) -> Branch | None:
        bid = self.table.selected_key()
        return branches.get_branch(self.db, bid) if bid else None

    def _selection_changed(self) -> None:
        b = self._selected()
        self.key_button.setText("Replace API key…" if b and b.has_api_key else "Create API key…")

    def _add(self) -> None:
        if BranchDialog(self.ctx, None, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _edit(self) -> None:
        b = self._selected()
        if b and BranchDialog(self.ctx, b, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _delete(self) -> None:
        b = self._selected()
        if b and confirm(self, f"Delete branch {b.name}? Only branches that were never used can be "
                               "deleted."):
            run_action(self, lambda: branches.delete_branch(self.db, b.id))
            self.ctx.notify()

    def _generate(self) -> None:
        b = self._selected()
        if not b:
            error(self, "Select a branch first")
            return
        if b.has_api_key and not confirm(
                self, f"Replace the order API key for {b.name}? Systems using the old key will stop "
                      "being able to send orders until you give them the new one."):
            return
        key = run_action(self, lambda: branches.generate_api_key(self.db, b.id))
        if key:
            self.ctx.notify()
            ApiKeyDialog(b, key, self).exec()

    def _revoke(self) -> None:
        b = self._selected()
        if b and b.has_api_key and confirm(
                self, f"Revoke the order API key for {b.name}? Orders can no longer be sent to this "
                      "branch."):
            run_action(self, lambda: branches.revoke_api_key(self.db, b.id))
            self.ctx.notify()


class BranchDialog(QDialog):
    def __init__(self, ctx: AppContext, branch: Branch | None, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.branch = branch or Branch()
        self.setWindowTitle("Edit branch" if branch else "Add branch")
        self.setMinimumWidth(420)
        b = self.branch
        form = QFormLayout(self)
        self.code, self.name = QLineEdit(b.code), QLineEdit(b.name)
        self.code.setMaxLength(16)
        self.code.setPlaceholderText("e.g. NORTH")
        self.address, self.phone = QLineEdit(b.address), QLineEdit(b.phone)
        self.active = QCheckBox("Active")
        self.active.setChecked(b.is_active)
        self.active.setEnabled(not b.is_head)
        for label, w in (("Code", self.code), ("Name", self.name), ("Address", self.address),
                         ("Phone", self.phone), ("", self.active)):
            form.addRow(label, w)
        if b.is_head:
            form.addRow("", muted("The head branch is always active and can't be deleted."))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        b = self.branch
        b.code, b.name = self.code.text(), self.name.text()
        b.address, b.phone, b.is_active = self.address.text(), self.phone.text(), self.active.isChecked()
        if run_action(self, lambda: branches.save_branch(self.ctx.db, b)):
            self.accept()


def example_request(key: str) -> str:
    return (
        "curl -X POST https://<your web server>/api/intake/orders \\\n"
        f'  -H "Authorization: Bearer {key}" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        "  -d '{\n"
        '    "reference": "SHOP-1001",\n'
        '    "fulfillment": "delivery",\n'
        '    "customer_name": "Dewi Lestari",\n'
        '    "address": "Jl. Merdeka 1, Bandung",\n'
        '    "phone": "0812-3456-7890",\n'
        '    "scheduled_date": "2026-10-02",\n'
        '    "ordered_at": "2026-10-01T09:30:00+07:00",\n'
        '    "notes": "Ring twice",\n'
        '    "items": [{ "sku": "P00007", "quantity": 2 }]\n'
        "  }'"
    )


class ApiKeyDialog(QDialog):
    """Shows a new API key once, with an example request."""

    def __init__(self, branch: Branch, key: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Order API key – {branch.name}")
        self.resize(640, 480)
        layout = QVBoxLayout(self)
        warning = heading("This is the only time the key is shown. Store it somewhere safe.", 11)
        warning.setStyleSheet("color: #c0392b")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        row = QHBoxLayout()
        self.key = QLineEdit(key)
        self.key.setReadOnly(True)
        row.addWidget(self.key, 1)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(key))
        row.addWidget(copy)
        layout.addLayout(row)
        layout.addWidget(heading("Send an order", 11))
        example = QPlainTextEdit(example_request(key))
        example.setReadOnly(True)
        example.setStyleSheet("font-family: monospace")
        layout.addWidget(example, 1)
        layout.addWidget(muted(
            "Orders are received by the web app's server (see web/README.md). “fulfillment” is "
            "“delivery” (default) or “pickup”; pickup orders don't need an address. Items use a "
            "product “sku” or “product_id”. Sending the same “reference” again returns the existing "
            "order instead of creating a duplicate."))
        buttons = QDialogButtonBox()
        buttons.addButton("I've saved the key", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
