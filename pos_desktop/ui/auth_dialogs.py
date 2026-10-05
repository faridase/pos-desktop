"""Sign-in, first-run setup, supervisor approval and password dialogs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
    QWidget,
)

from ..core import auth, branches
from ..core import settings as st
from ..core.auth import ROLES, AuthError, User
from ..core.db import Database
from .common import AppContext, error, heading, muted, run_action


def _password_field(placeholder: str = "") -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.Password)
    field.setPlaceholderText(placeholder)
    return field


def _error_label() -> QLabel:
    label = QLabel()
    label.setStyleSheet("color: #c0392b")
    label.setWordWrap(True)
    label.hide()
    return label


class LoginDialog(QDialog):
    """Signs a staff member in. `user` is set on success."""

    def __init__(self, db: Database, parent: QWidget | None = None, locked_by: User | None = None,
                 quit_label: str = "Quit") -> None:
        super().__init__(parent)
        self.db = db
        self.user: User | None = None
        self.setWindowTitle("Sign in")
        self.setMinimumWidth(360)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        layout = QVBoxLayout(self)
        layout.addWidget(heading(st.get(db, "store_name"), 18))
        layout.addWidget(muted(f"Locked by {locked_by.full_name}. Sign in to continue."
                               if locked_by else "Sign in to start."))
        form = QFormLayout()
        self.username = QLineEdit(locked_by.username if locked_by else "")
        self.password = _password_field("Password or PIN")
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        layout.addLayout(form)
        self.error = _error_label()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox()
        self.ok = buttons.addButton("Sign in", QDialogButtonBox.AcceptRole)
        self.ok.setObjectName("primary")
        buttons.addButton(quit_label, QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        (self.password if locked_by else self.username).setFocus()

    def submit(self) -> None:
        try:
            self.user = auth.authenticate(self.db, self.username.text(), self.password.text())
        except AuthError as e:
            self.error.setText(str(e))
            self.error.show()
            self.password.clear()
            self.password.setFocus()
            return
        self.accept()


class SetupOwnerDialog(QDialog):
    """First run (or first run after upgrading): create the owner account."""

    def __init__(self, db: Database, upgraded: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.user: User | None = None
        self.setWindowTitle("Create the owner account")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Create the owner account", 15))
        layout.addWidget(muted(
            ("Staff sign-in is new in this version. Your existing data is safe. " if upgraded else "")
            + "This first account is the owner/admin. It can add cashiers and managers "
              "under Staff."))
        form = QFormLayout()
        self.full_name = QLineEdit()
        self.username = QLineEdit("owner")
        self.password = _password_field(f"At least {auth.MIN_PASSWORD_LENGTH['admin']} characters")
        self.confirm = _password_field()
        form.addRow("Your name", self.full_name)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        form.addRow("Confirm password", self.confirm)
        layout.addLayout(form)
        self.error = _error_label()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok = buttons.addButton("Create account", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.full_name.setFocus()

    def submit(self) -> None:
        if self.password.text() != self.confirm.text():
            self.error.setText("Passwords don't match")
            self.error.show()
            return
        try:
            uid = auth.create_user(self.db, self.username.text(), self.full_name.text(), "admin",
                                   self.password.text())
            self.user = auth.authenticate(self.db, self.username.text(), self.password.text())
        except AuthError as e:
            self.error.setText(str(e))
            self.error.show()
            return
        assert self.user.id == uid
        self.accept()


class OverrideDialog(QDialog):
    """A supervisor enters their credentials to approve one action."""

    def __init__(self, ctx: AppContext, permission: str, action: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx, self.permission, self.action = ctx, permission, action
        self.approver: User | None = None
        self.setWindowTitle("Manager approval required")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Manager approval required", 13))
        layout.addWidget(muted(f"{action}\n\nA manager or owner must enter their "
                               "credentials to approve this."))
        form = QFormLayout()
        self.username = QLineEdit()
        self.password = _password_field()
        form.addRow("Manager username", self.username)
        form.addRow("Password", self.password)
        layout.addLayout(form)
        self.error = _error_label()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.addButton("Approve", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.username.setFocus()

    def submit(self) -> None:
        try:
            self.approver = auth.authorize(self.ctx.db, self.username.text(),
                                           self.password.text(), self.permission, self.action)
        except AuthError as e:
            self.error.setText(str(e))
            self.error.show()
            self.password.clear()
            return
        self.accept()


def request_permission(parent: QWidget, ctx: AppContext, permission: str,
                       action: str) -> int | None:
    """Returns the id of whoever authorised `permission`: the signed-in user
    if they have it, otherwise a supervisor who approves on the spot. None
    if nobody approved."""
    if ctx.can(permission):
        return ctx.user.id
    dialog = OverrideDialog(ctx, permission, action, parent)
    if dialog.exec() == QDialog.Accepted and dialog.approver:
        return dialog.approver.id
    return None


class ChangePasswordDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Change my password")
        form = QFormLayout(self)
        self.current = _password_field()
        self.new = _password_field(
            f"At least {auth.MIN_PASSWORD_LENGTH[ctx.user.role]} characters")
        self.confirm = _password_field()
        form.addRow("Current password", self.current)
        form.addRow("New password", self.new)
        form.addRow("Confirm new password", self.confirm)
        self.error = _error_label()
        form.addRow(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def submit(self) -> None:
        if self.new.text() != self.confirm.text():
            self.error.setText("New passwords don't match")
            self.error.show()
            return
        try:
            auth.change_own_password(self.ctx.db, self.ctx.user.id, self.current.text(),
                                     self.new.text())
        except AuthError as e:
            self.error.setText(str(e))
            self.error.show()
            return
        self.accept()


class UserDialog(QDialog):
    """Add a staff member, or edit name/role/status of an existing one."""

    def __init__(self, ctx: AppContext, user: User | None, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx, self.user = ctx, user
        self.setWindowTitle("Edit staff member" if user else "Add staff member")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        self.full_name = QLineEdit(user.full_name if user else "")
        self.username = QLineEdit(user.username if user else "")
        self.username.setEnabled(user is None)
        self.role = QComboBox()
        for key, label in ROLES.items():
            self.role.addItem(label, key)
        self.role.setCurrentIndex(self.role.findData(user.role if user else "cashier"))
        self.role_help = muted()
        self.role.currentIndexChanged.connect(self._describe_role)
        form.addRow("Full name", self.full_name)
        form.addRow("Username", self.username)
        form.addRow("Role", self.role)
        form.addRow("", self.role_help)
        self.branch = QComboBox()
        current = branches.branch_of_user(ctx.db, user) if user else branches.current_branch(ctx.db).id
        for b in branches.list_branches(ctx.db):
            if b.is_active or b.id == current:
                self.branch.addItem(b.name, b.id)
        self.branch.setCurrentIndex(max(0, self.branch.findData(current)))
        form.addRow("Branch", self.branch)
        self.handles_orders = QCheckBox("Handles customer orders")
        self.handles_orders.setChecked(bool(user and user.handles_orders))
        self.handles_orders.setToolTip("Can view, take and complete delivery and pickup orders "
                                       "for their branch")
        form.addRow("", self.handles_orders)
        if user is None:
            self.password = _password_field()
            self.confirm = _password_field()
            form.addRow("Password / PIN", self.password)
            form.addRow("Confirm", self.confirm)
        else:
            self.active = QComboBox()
            self.active.addItem("Active", True)
            self.active.addItem("Deactivated (can't sign in)", False)
            self.active.setCurrentIndex(0 if user.is_active else 1)
            form.addRow("Status", self.active)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._describe_role()

    def _describe_role(self) -> None:
        role = self.role.currentData()
        perms = [auth.PERMISSION_LABELS[p] for p in auth.PERMISSION_LABELS
                 if p in auth.PERMISSIONS[role]]
        minimum = auth.MIN_PASSWORD_LENGTH[role]
        self.role_help.setText("Can: " + "; ".join(perms).lower()
                               + f".\nPassword/PIN: at least {minimum} characters.")
        if hasattr(self, "handles_orders"):
            admin = role == "admin"
            self.handles_orders.setEnabled(not admin)
            if admin:
                self.handles_orders.setChecked(True)

    def submit(self) -> None:
        if self.user is None:
            if self.password.text() != self.confirm.text():
                error(self, "Passwords don't match")
                return
            ok = run_action(self, lambda: auth.create_user(
                self.ctx.db, self.username.text(), self.full_name.text(),
                self.role.currentData(), self.password.text(), self.branch.currentData(),
                self.handles_orders.isChecked()))
        else:
            ok = run_action(self, lambda: auth.update_user(
                self.ctx.db, self.user.id, self.full_name.text(), self.role.currentData(),
                self.active.currentData(), self.branch.currentData(),
                self.handles_orders.isChecked()) or True)
        if ok:
            self.accept()


class ResetPasswordDialog(QDialog):
    def __init__(self, ctx: AppContext, user: User, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx, self.user = ctx, user
        self.setWindowTitle(f"Reset password – {user.full_name}")
        form = QFormLayout(self)
        form.addRow(muted("Sets a new password and unlocks the account. Tell the staff "
                          "member to change it after signing in."))
        self.password = _password_field(
            f"At least {auth.MIN_PASSWORD_LENGTH[user.role]} characters")
        self.confirm = _password_field()
        form.addRow("New password / PIN", self.password)
        form.addRow("Confirm", self.confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def submit(self) -> None:
        if self.password.text() != self.confirm.text():
            error(self, "Passwords don't match")
            return
        if run_action(self, lambda: auth.set_password(self.ctx.db, self.user.id,
                                                      self.password.text()) or True):
            self.accept()
