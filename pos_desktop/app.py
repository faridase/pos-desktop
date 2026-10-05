"""Application entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from .core import auth
from .core.db import Database, NewerDatabaseError
from .core.sample_data import load_sample_data
from .ui.auth_dialogs import LoginDialog, SetupOwnerDialog
from .ui.common import AppContext
from .ui.main_window import MainWindow

RESOURCES = Path(__file__).parent / "resources"

STYLE = """
#sidebar {
    border: none;
    background: palette(window);
    border-right: 1px solid palette(mid);
    padding-top: 8px;
    font-size: 11pt;
}
#sidebar::item { padding: 6px 14px; border-radius: 6px; margin: 1px 8px; }
#sidebar::item:selected { background: palette(highlight); color: palette(highlighted-text); }
#sidebar::item:hover:!selected { background: palette(midlight); }
QFrame#card { border: 1px solid palette(mid); border-radius: 8px; background: palette(base); }
QPushButton#primary {
    background: palette(highlight); color: palette(highlighted-text);
    border: none; border-radius: 5px; padding: 6px 16px; font-weight: bold;
}
QPushButton#primary:disabled { background: palette(mid); }
QPushButton#primary:hover { border: 1px solid palette(highlighted-text); }
QLabel#muted { color: palette(placeholder-text); }
#productGrid::item {
    border: 1px solid palette(mid); border-radius: 8px; background: palette(base);
}
#productGrid::item:hover { border: 1px solid palette(highlight); }
QTabWidget::pane { border-top: 1px solid palette(mid); }
"""


def default_db_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    return Path(base or Path.home() / ".mobile-pos-desktop") / "pos.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mobile POS Desktop")
    parser.add_argument("--db", help="Path to the data file (default: per-user app data folder)")
    parser.add_argument("--sample", action="store_true", help="Load sample data into a new file")
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Mobile POS Desktop")
    app.setOrganizationName("MobilePOS")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    icon = RESOURCES / "icon.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    path = Path(args.db or os.environ.get("POS_DB") or default_db_path())
    is_new = not path.exists()
    try:
        db = Database(path)
    except NewerDatabaseError as e:
        QMessageBox.critical(None, "Please update the app", str(e))
        return 1

    ctx = AppContext(db)
    if not auth.has_users(db):
        # Brand-new file, or one from before staff sign-in existed.
        setup = SetupOwnerDialog(db, upgraded=not is_new)
        if setup.exec() != QDialog.Accepted:
            db.close()
            return 0
        ctx.login(setup.user)
        if is_new and (args.sample or QMessageBox.question(
            None, "Welcome",
            "Welcome to Mobile POS Desktop!\n\nLoad sample products and three weeks of demo "
            "sales so you can explore? Choose No to start with empty books.",
        ) == QMessageBox.Yes):
            load_sample_data(db)
    else:
        login = LoginDialog(db)
        if login.exec() != QDialog.Accepted or login.user is None:
            db.close()
            return 0
        ctx.login(login.user)

    window = MainWindow(ctx)
    window.show()
    code = app.exec()
    window.ctx.db.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
