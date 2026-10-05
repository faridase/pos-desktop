"""Staff accounts and the audit log (owner/admin only)."""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QTabWidget, QVBoxLayout, QWidget

from ..core import auth, branches
from .auth_dialogs import ResetPasswordDialog, UserDialog
from .common import (
    AppContext, DataTable, DateRangeBar, Page, button, export_buttons, muted, page_layout,
)

ACTIONS = {
    "login": "Signed in", "logout": "Signed out", "login_failed": "Failed sign-in",
    "lock": "Screen locked",
    "override": "Manager approval", "override_failed": "Failed approval",
    "override_denied": "Approval refused", "user_created": "Staff added",
    "user_updated": "Staff changed", "password_reset": "Password reset",
    "password_changed": "Password changed", "backup": "Backup", "restore": "Restore",
    "settings": "Settings changed",
    "branch_created": "Branch added", "branch_updated": "Branch changed",
    "branch_deleted": "Branch deleted", "api_key_generated": "Order API key created",
    "api_key_revoked": "Order API key revoked", "order_received": "Order received",
    "order_updated": "Order edited", "order_deleted": "Order deleted",
    "order_picked_up": "Order taken", "order_released": "Order released",
    "order_completed": "Order completed", "order_cancelled": "Order cancelled",
}


class UsersPage(Page):
    title = "Staff"
    permission = "users"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        layout = page_layout(self, "Staff", "Who can sign in, and what each role may do.")
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        users = QWidget()
        v = QVBoxLayout(users)
        self.table = DataTable(["Name", "Username", "Role", "Branch", "Orders", "Status",
                                "Last sign-in"], stretch=0)
        self.table.activated_key.connect(lambda _: self._edit())
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(button("Add staff member…", self._add, primary=True))
        row.addWidget(button("Edit…", self._edit))
        row.addWidget(button("Reset password…", self._reset))
        row.addStretch(1)
        v.addLayout(row)
        roles = QLabel(self._roles_html())
        roles.setWordWrap(True)
        v.addWidget(roles)
        self.tabs.addTab(users, "Staff accounts")

        audit = QWidget()
        v = QVBoxLayout(audit)
        bar = QHBoxLayout()
        self.range = DateRangeBar("Last 7 days")
        self.range.changed.connect(self._refresh_audit)
        self.who = QComboBox()
        self.who.currentIndexChanged.connect(self._refresh_audit)
        bar.addWidget(self.range)
        bar.addWidget(QLabel("Staff"))
        bar.addWidget(self.who)
        bar.addStretch(1)
        v.addLayout(bar)
        self.audit = DataTable(["Time", "Staff", "Event", "Details"], stretch=3)
        v.addWidget(self.audit, 1)
        v.addWidget(muted("Sales, refunds, shifts and journal entries also record who made them."))
        v.addLayout(export_buttons(self, self.audit, lambda: "Audit log", self.range.label))
        self.tabs.addTab(audit, "Audit log")

    @staticmethod
    def _roles_html() -> str:
        rows = []
        for role, label in auth.ROLES.items():
            perms = [auth.PERMISSION_LABELS[p] for p in auth.PERMISSION_LABELS
                     if p in auth.PERMISSIONS[role]]
            rows.append(f"<p><b>{label}</b>: {', '.join(perms).lower()}.</p>")
        return ("<p style='margin-top:8px'>Cashiers can still give discounts or refunds when a "
                "manager approves on the spot with their own sign-in.</p>" + "".join(rows))

    def refresh(self) -> None:
        users = auth.list_users(self.db)
        names = {b.id: b.name for b in branches.list_branches(self.db)}
        colors = {n: QColor("gray") for n, u in enumerate(users) if not u.is_active}
        self.table.set_rows([
            [u.full_name + ("  (you)" if self.ctx.user and u.id == self.ctx.user.id else ""),
             u.username, u.role_label, names.get(branches.branch_of_user(self.db, u), ""),
             "All branches" if u.can("orders") else ("Yes" if u.handles_orders else ""),
             "Active" if u.is_active else "Deactivated",
             u.last_login_at or "Never"] for u in users], [u.id for u in users], colors=colors)
        current = self.who.currentData()
        self.who.blockSignals(True)
        self.who.clear()
        self.who.addItem("Everyone", None)
        for u in users:
            self.who.addItem(u.full_name, u.id)
        self.who.setCurrentIndex(max(0, self.who.findData(current)))
        self.who.blockSignals(False)
        self._refresh_audit()

    def _refresh_audit(self) -> None:
        start, end = self.range.range()
        rows = auth.audit_entries(self.db, start, end, self.who.currentData())
        failed = {n: QColor("#c0392b") for n, r in enumerate(rows)
                  if r["action"].endswith(("_failed", "_denied"))}
        self.audit.set_rows([
            [r["created_at"], r["full_name"] or "", ACTIONS.get(r["action"], r["action"]),
             r["details"]] for r in rows], colors=failed)

    def _selected(self) -> auth.User | None:
        uid = self.table.selected_key()
        return auth.get_user(self.db, uid) if uid else None

    def _add(self) -> None:
        if UserDialog(self.ctx, None, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _edit(self) -> None:
        user = self._selected()
        if user and UserDialog(self.ctx, user, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _reset(self) -> None:
        user = self._selected()
        if user and ResetPasswordDialog(self.ctx, user, self).exec() == QDialog.Accepted:
            self.ctx.notify()
