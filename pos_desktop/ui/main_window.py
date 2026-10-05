"""Main window: sidebar navigation filtered by role, sign-in/lock, menus,
backup/restore."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QStackedWidget, QWidget,
)

from .. import __version__
from ..core import auth
from ..core import settings as st
from ..core.db import Database, NewerDatabaseError
from ..core.sample_data import load_sample_data
from ..core import branches, orders
from .accounting_page import AccountingPage
from .branches_page import BranchesPage
from .auth_dialogs import ChangePasswordDialog, LoginDialog
from .common import AppContext, Page, confirm, error, info
from .dashboard import DashboardPage
from .expenses_page import ExpensesPage
from .inventory_page import InventoryPage
from .orders_page import OrdersPage
from .parties_page import PartiesPage
from .products_page import ProductsPage
from .register import RegisterPage
from .reports_page import ReportsPage
from .sales_page import SalesPage
from .settings_page import SettingsPage
from .users_page import UsersPage


class IdleWatcher(QObject):
    """Calls `on_idle` after `minutes` without keyboard or mouse input."""

    ACTIVITY = {QEvent.KeyPress, QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.Wheel}

    def __init__(self, on_idle) -> None:
        super().__init__()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(on_idle)

    def set_minutes(self, minutes: int) -> None:
        if minutes > 0:
            self.timer.setInterval(minutes * 60_000)
            self.timer.start()
        else:
            self.timer.stop()

    def eventFilter(self, obj, event) -> bool:
        if event.type() in self.ACTIVITY and self.timer.interval() and self.timer.isActive():
            self.timer.start()
        return False


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        if ctx.user is None:
            raise RuntimeError("Sign a user in before opening the main window")
        self.ctx = ctx
        self.resize(1280, 800)
        self.setMinimumSize(QSize(960, 620))
        self._locking = False

        self.pages: list[Page] = [
            DashboardPage(ctx),
            RegisterPage(ctx),
            OrdersPage(ctx),
            SalesPage(ctx),
            ProductsPage(ctx),
            InventoryPage(ctx),
            PartiesPage(ctx, "customers"),
            PartiesPage(ctx, "suppliers"),
            ExpensesPage(ctx),
            AccountingPage(ctx),
            ReportsPage(ctx),
            UsersPage(ctx),
            BranchesPage(ctx),
            SettingsPage(ctx, self),
        ]

        self.nav = QListWidget()
        self.nav.setObjectName("sidebar")
        self.nav.setFixedWidth(180)
        self.nav.setFocusPolicy(Qt.NoFocus)
        self.stack = QStackedWidget()
        for page in self.pages:
            item = QListWidgetItem(page.title)
            item.setSizeHint(QSize(160, 38))
            self.nav.addItem(item)
            self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self._build_menu()
        self._build_status_bar()

        # New orders can arrive through the web app's order API at any time.
        self.orders_timer = QTimer(self)
        self.orders_timer.setInterval(30_000)
        self.orders_timer.timeout.connect(self._update_orders_badge)
        self.orders_timer.start()

        self.idle = IdleWatcher(self.lock)
        QApplication.instance().installEventFilter(self.idle)

        ctx.data_changed.connect(self._data_changed)
        ctx.settings_changed.connect(self._update_title)
        ctx.settings_changed.connect(self._apply_idle_setting)
        ctx.user_changed.connect(self._apply_user)
        self._apply_user()
        self._apply_idle_setting()
        self._update_title()

    # ---- building -------------------------------------------------------------------

    def _action(self, menu, text: str, slot, shortcut=None, permission: str = "") -> QAction:
        action = QAction(text, self)
        action.triggered.connect(slot)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setData(permission)
        menu.addAction(action)
        return action

    def _build_menu(self) -> None:
        self.gated_actions: list[QAction] = []
        file_menu = self.menuBar().addMenu("&File")
        for text, slot, key in (("&Back up data…", self.backup, "Ctrl+B"),
                                ("&Restore from backup…", self.restore, None),
                                ("Load &sample data…", self.load_sample, None)):
            self.gated_actions.append(self._action(file_menu, text, slot, key, "backup"))
        file_menu.addSeparator()
        self._action(file_menu, "&Lock screen", self.lock, "Ctrl+L")
        self._action(file_menu, "S&witch user…", self.switch_user, "Ctrl+Shift+L")
        self._action(file_menu, "&Change my password…",
                     lambda: ChangePasswordDialog(self.ctx, self).exec() and
                     info(self, "Password changed."))
        file_menu.addSeparator()
        self._action(file_menu, "&Quit", self.close, QKeySequence.Quit)

        self.go_menu = self.menuBar().addMenu("&Go")
        self.go_actions: list[QAction] = []
        for n, page in enumerate(self.pages):
            action = self._action(self.go_menu, page.title,
                                  lambda _=False, i=n: self.nav.setCurrentRow(i),
                                  permission=page.permission)
            self.go_actions.append(action)

        help_menu = self.menuBar().addMenu("&Help")
        self._action(help_menu, "Keyboard shortcuts", lambda: info(self, (
            "Register\n"
            "  F2  Focus scan / search\n"
            "  Enter  Add scanned barcode or SKU\n"
            "  F8  Hold order\n"
            "  F12 or Ctrl+Enter  Charge\n"
            "  Delete  Remove selected line\n\n"
            "General\n"
            "  Ctrl+1 … Ctrl+9  Switch screens\n"
            "  Ctrl+L  Lock screen\n"
            "  Ctrl+Shift+L  Switch user\n"
            "  Ctrl+B  Back up data (admin)"), "Keyboard shortcuts"))
        self._action(help_menu, "About", lambda: QMessageBox.about(
            self, "About Mobile POS Desktop",
            f"<b>Mobile POS Desktop</b> {__version__}<br>"
            "Point of sale with inventory and double-entry accounting.<br><br>"
            f"Data file:<br><code>{self.ctx.db.path}</code>"))

    def _build_status_bar(self) -> None:
        self.branch_label = QLabel()
        self.branch_label.setToolTip("The branch this POS runs as")
        self.statusBar().addPermanentWidget(self.branch_label)
        self.user_label = QLabel()
        lock = QPushButton("Lock")
        lock.setToolTip("Lock the screen (Ctrl+L)")
        lock.clicked.connect(self.lock)
        switch = QPushButton("Switch user")
        switch.clicked.connect(self.switch_user)
        for w in (self.user_label, switch, lock):
            self.statusBar().addPermanentWidget(w)

    # ---- session ----------------------------------------------------------------------

    def _apply_user(self) -> None:
        """Shows only what the signed-in role may use."""
        user = self.ctx.user
        if user is None:
            return
        self.user_label.setText(f"Signed in: <b>{user.full_name}</b> ({user.role_label})  ")
        self.branch_label.setText(f"Branch: {branches.current_branch(self.ctx.db).name}  ")
        visible = []
        for n, page in enumerate(self.pages):
            allowed = self.ctx.can(page.permission)
            self.nav.setRowHidden(n, not allowed)
            if allowed:
                visible.append(n)
        shortcut_no = 1
        for n, action in enumerate(self.go_actions):
            allowed = n in visible
            action.setVisible(allowed)
            action.setShortcut(QKeySequence(f"Ctrl+{shortcut_no}") if allowed and shortcut_no <= 9
                               else QKeySequence())
            shortcut_no += allowed
        for action in self.gated_actions:
            action.setVisible(self.ctx.can(action.data()))
        # Start on the register when allowed, otherwise the first allowed page.
        if self.nav.currentRow() not in visible:
            self.nav.setCurrentRow(1 if 1 in visible else visible[0])
        self._data_changed()

    def _sign_in(self, locked_by: auth.User | None, quit_label: str) -> bool:
        """Blocks the window until someone signs in. False if they chose to quit."""
        self.centralWidget().hide()
        self.menuBar().setEnabled(False)
        try:
            while True:
                dialog = LoginDialog(self.ctx.db, self, locked_by, quit_label)
                if dialog.exec() == QDialog.Accepted and dialog.user:
                    self.ctx.login(dialog.user)
                    return True
                if confirm(self, "Quit Mobile POS?"):
                    return False
        finally:
            self.menuBar().setEnabled(True)
            self.centralWidget().show()

    def lock(self) -> None:
        if self._locking or self.ctx.user is None:
            return
        self._locking = True
        previous = self.ctx.user
        with self.ctx.db.transaction():
            auth.log(self.ctx.db, "lock", previous.username)
        try:
            if not self._sign_in(previous, "Quit"):
                self.close()
                return
            if self.ctx.user.id != previous.id:
                self.statusBar().showMessage(f"Switched to {self.ctx.user.full_name}", 4000)
        finally:
            self._locking = False
            self._apply_idle_setting()

    def switch_user(self) -> None:
        if self._locking:
            return
        self._locking = True
        try:
            self.ctx.logout()
            if not self._sign_in(None, "Quit"):
                self.close()
        finally:
            self._locking = False
            self._apply_idle_setting()

    def _apply_idle_setting(self) -> None:
        self.idle.set_minutes(st.get_int(self.ctx.db, "auto_lock_minutes"))

    def _update_title(self) -> None:
        self.setWindowTitle(f"{st.get(self.ctx.db, 'store_name')} – Mobile POS Desktop")

    def _data_changed(self) -> None:
        for page in self.pages:
            page.mark_dirty()
        self._update_orders_badge()

    def _update_orders_badge(self) -> None:
        """Shows how many new orders are waiting, e.g. "Orders (2)"."""
        user = self.ctx.user
        for n, page in enumerate(self.pages):
            if not isinstance(page, OrdersPage):
                continue
            count = 0
            if user is not None and user.can("handle_orders"):
                branch = None if user.can("orders") else branches.branch_of_user(self.ctx.db, user)
                count = orders.count_new(self.ctx.db, branch)
            self.nav.item(n).setText(f"{page.title} ({count})" if count else page.title)

    def closeEvent(self, event) -> None:
        if self.ctx.user is not None:
            self.ctx.logout()
        QApplication.instance().removeEventFilter(self.idle)
        super().closeEvent(event)

    # ---- data management ------------------------------------------------------------

    def _require(self, permission: str) -> bool:
        if self.ctx.can(permission):
            return True
        error(self, "Your role doesn't allow this.")
        return False

    def backup(self) -> None:
        if not self._require("backup"):
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        path, _ = QFileDialog.getSaveFileName(
            self, "Back up data", f"pos-backup-{stamp}.db", "POS data (*.db);;All files (*)")
        if not path:
            return
        try:
            self.ctx.db.backup_to(path)
        except Exception as e:  # noqa: BLE001 - show any I/O problem to the user
            error(self, f"Backup failed: {e}")
            return
        with self.ctx.db.transaction():
            auth.log(self.ctx.db, "backup", path)
        info(self, f"Backup saved to\n{path}")

    def restore(self) -> None:
        if not self._require("backup"):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Restore from backup", "", "POS data (*.db);;All files (*)")
        if not path:
            return
        if not confirm(self, "Replace all current data with this backup? A copy of the "
                             "current data is kept next to it as “.before-restore”.\n\n"
                             "You'll need to sign in again with an account from the backup."):
            return
        current = Path(self.ctx.db.path)
        safety = current.with_suffix(current.suffix + ".before-restore")
        staging = current.with_suffix(current.suffix + ".restoring")
        try:
            self.ctx.db.backup_to(safety)
            # Validate (and upgrade, if older) a temporary copy first.
            shutil.copyfile(path, staging)
            Database(staging).close()
        except NewerDatabaseError as e:
            staging.unlink(missing_ok=True)
            error(self, str(e))
            return
        except Exception as e:  # noqa: BLE001
            staging.unlink(missing_ok=True)
            error(self, f"That file isn't a valid POS backup: {e}")
            return
        with self.ctx.db.transaction():
            auth.log(self.ctx.db, "restore", path)
        self.ctx.logout()
        self.ctx.db.close()
        for suffix in ("-wal", "-shm"):
            Path(str(current) + suffix).unlink(missing_ok=True)
        shutil.move(str(staging), str(current))
        self.ctx.db = Database(current)
        self.ctx.reload_settings()
        info(self, "Backup restored. Please sign in.")
        if not self._ensure_account_after_restore():
            self.close()

    def _ensure_account_after_restore(self) -> bool:
        from .auth_dialogs import SetupOwnerDialog
        if not auth.has_users(self.ctx.db):
            dialog = SetupOwnerDialog(self.ctx.db, upgraded=True, parent=self)
            if dialog.exec() != QDialog.Accepted:
                return False
            self.ctx.login(dialog.user)
            return True
        return self._sign_in(None, "Quit")

    def load_sample(self) -> None:
        if not self._require("backup"):
            return
        if not confirm(self, "Add demo products, suppliers, customers and three weeks of "
                             "sample sales? This writes real entries to your books, so only "
                             "do this in a practice data file."):
            return
        load_sample_data(self.ctx.db)
        self.ctx.notify()
        self.statusBar().showMessage("Sample data loaded", 4000)
