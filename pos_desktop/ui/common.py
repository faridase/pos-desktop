"""Shared UI building blocks."""

from __future__ import annotations

import csv
from datetime import date, timedelta
from typing import Any, Callable, Iterable

from PySide6.QtCore import QDate, QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextDocument
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrintPreviewDialog
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSizePolicy,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import accounting as acc
from ..core import auth
from ..core import settings as st
from ..core.auth import User
from ..core.db import Database
from ..core.money import MoneyFormat

RIGHT = Qt.AlignRight | Qt.AlignVCenter
LEFT = Qt.AlignLeft | Qt.AlignVCenter
CENTER = Qt.AlignCenter


class AppContext(QObject):
    """Holds the open database and tells pages when data changes."""

    data_changed = Signal()
    settings_changed = Signal()
    user_changed = Signal()

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.fmt: MoneyFormat = st.money_format(db)
        self.user: User | None = None

    # ---- session ------------------------------------------------------------------

    def can(self, permission: str) -> bool:
        return self.user is not None and self.user.can(permission)

    def login(self, user: User) -> None:
        self.user = user
        self.db.user_id = user.id
        self.user_changed.emit()

    def logout(self) -> None:
        if self.user is not None:
            with self.db.transaction():
                auth.log(self.db, "logout", self.user.username)
        self.user = None
        self.db.user_id = None
        self.user_changed.emit()

    def money(self, amount: int | None) -> str:
        return "" if amount is None else self.fmt.format(amount)

    def notify(self) -> None:
        self.data_changed.emit()

    def reload_settings(self) -> None:
        self.fmt = st.money_format(self.db)
        self.settings_changed.emit()
        self.data_changed.emit()


# ---- messages -----------------------------------------------------------------


def error(parent: QWidget, message: str, title: str = "Can't do that") -> None:
    QMessageBox.warning(parent, title, message)


def info(parent: QWidget, message: str, title: str = "Done") -> None:
    QMessageBox.information(parent, title, message)


def confirm(parent: QWidget, message: str, title: str = "Please confirm") -> bool:
    return QMessageBox.question(parent, title, message) == QMessageBox.Yes


def run_action(parent: QWidget, fn: Callable[[], Any]) -> Any:
    """Runs a core action, showing validation errors instead of crashing."""
    try:
        return fn()
    except ValueError as e:  # all core errors subclass ValueError
        error(parent, str(e))
    return None


# ---- inputs -------------------------------------------------------------------


class MoneyEdit(QLineEdit):
    def __init__(self, ctx: AppContext, value: int | None = None, allow_negative: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.allow_negative = allow_negative
        self.setAlignment(RIGHT)
        self.setPlaceholderText(ctx.fmt.to_input(0))
        self.setMinimumWidth(90)
        if value is not None:
            self.set_value(value)

    def value(self) -> int | None:
        if not self.text().strip():
            return 0
        return self.ctx.fmt.parse(self.text(), self.allow_negative)

    def set_value(self, amount: int) -> None:
        self.setText(self.ctx.fmt.to_input(amount))


def date_edit(value: date | None = None) -> QDateEdit:
    edit = QDateEdit()
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("yyyy-MM-dd")
    d = value or date.today()
    edit.setDate(QDate(d.year, d.month, d.day))
    return edit


def iso(edit: QDateEdit) -> str:
    return edit.date().toString("yyyy-MM-dd")


def account_combo(accounts: Iterable[acc.Account], selected_id: int | None = None) -> QComboBox:
    combo = QComboBox()
    for a in accounts:
        combo.addItem(a.label, a.id)
    if selected_id is not None:
        combo.setCurrentIndex(max(0, combo.findData(selected_id)))
    return combo


class DateRangeBar(QWidget):
    """Preset + custom date range."""

    changed = Signal()
    PRESETS = ["Today", "Yesterday", "Last 7 days", "This month", "Last month",
               "Last 30 days", "This year", "All time", "Custom"]

    def __init__(self, default: str = "This month", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.preset = QComboBox()
        self.preset.addItems(self.PRESETS)
        self.start = date_edit()
        self.end = date_edit()
        layout.addWidget(QLabel("Period"))
        layout.addWidget(self.preset)
        layout.addWidget(self.start)
        layout.addWidget(QLabel("to"))
        layout.addWidget(self.end)
        self.preset.currentTextChanged.connect(self._apply_preset)
        self.start.dateChanged.connect(self._custom)
        self.end.dateChanged.connect(self._custom)
        self._applying = False
        self.preset.setCurrentText(default)
        self._apply_preset(default)

    def _apply_preset(self, name: str) -> None:
        today = date.today()
        first = today.replace(day=1)
        ranges = {
            "Today": (today, today),
            "Yesterday": (today - timedelta(days=1),) * 2,
            "Last 7 days": (today - timedelta(days=6), today),
            "This month": (first, today),
            "Last month": ((first - timedelta(days=1)).replace(day=1), first - timedelta(days=1)),
            "Last 30 days": (today - timedelta(days=29), today),
            "This year": (today.replace(month=1, day=1), today),
            "All time": (date(2000, 1, 1), today),
        }
        if name not in ranges:
            return
        start, end = ranges[name]
        self._applying = True
        self.start.setDate(QDate(start.year, start.month, start.day))
        self.end.setDate(QDate(end.year, end.month, end.day))
        self._applying = False
        self.changed.emit()

    def _custom(self) -> None:
        if self._applying:
            return
        self.preset.blockSignals(True)
        self.preset.setCurrentText("Custom")
        self.preset.blockSignals(False)
        self.changed.emit()

    def range(self) -> tuple[str, str]:
        start, end = iso(self.start), iso(self.end)
        return (start, end) if start <= end else (end, start)

    def label(self) -> str:
        start, end = self.range()
        return start if start == end else f"{start} to {end}"


# ---- tables -------------------------------------------------------------------


class SortItem(QTableWidgetItem):
    def __init__(self, text: str, sort_key: Any) -> None:
        super().__init__(text)
        self.sort_key = sort_key

    def __lt__(self, other: QTableWidgetItem) -> bool:
        other_key = getattr(other, "sort_key", other.text())
        try:
            return self.sort_key < other_key
        except TypeError:
            return str(self.sort_key) < str(other_key)


class DataTable(QTableWidget):
    """Read-only table. Rows are lists of cells; a cell is text, or a
    (text, sort_key) tuple. Each row can carry a key (e.g. a record id)."""

    activated_key = Signal(object)

    def __init__(self, columns: list[str], right_align: Iterable[int] = (),
                 stretch: int | None = None, sortable: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(0, len(columns), parent)
        self.columns = columns
        self.right_align = set(right_align)
        self._keys: list[Any] = []
        self.setHorizontalHeaderLabels(columns)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(26)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(stretch is None)
        if stretch is not None:
            header.setSectionResizeMode(stretch, QHeaderView.Stretch)
        self.setSortingEnabled(sortable)
        self.cellDoubleClicked.connect(lambda r, _c: self.activated_key.emit(self.key_at(r)))

    def set_rows(self, rows: list[list[Any]], keys: list[Any] | None = None,
                 bold_rows: Iterable[int] = (), colors: dict[int, QColor] | None = None) -> None:
        sorting = self.isSortingEnabled()
        self.setSortingEnabled(False)
        selected = self.selected_key()
        self.clearContents()
        self.setRowCount(len(rows))
        self._keys = list(keys) if keys is not None else [None] * len(rows)
        bold = set(bold_rows)
        colors = colors or {}
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                text, key = (cell if isinstance(cell, tuple) else (cell, None))
                text = "" if text is None else str(text)
                item = SortItem(text, key if key is not None else text.lower())
                item.setData(Qt.UserRole, r)
                item.setTextAlignment(RIGHT if c in self.right_align else LEFT)
                if r in bold:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                if r in colors:
                    item.setForeground(colors[r])
                self.setItem(r, c, item)
        self.setSortingEnabled(sorting)
        header = self.horizontalHeader()
        for c in range(self.columnCount()):
            # Resizing a Stretch column to contents would stop it stretching.
            if header.sectionResizeMode(c) != QHeaderView.Stretch:
                self.resizeColumnToContents(c)
        if selected is not None:
            self.select_key(selected)

    def key_at(self, row: int) -> Any:
        item = self.item(row, 0)
        if item is None:
            return None
        index = item.data(Qt.UserRole)
        return self._keys[index] if index is not None and index < len(self._keys) else None

    def selected_key(self) -> Any:
        rows = self.selectionModel().selectedRows() if self.selectionModel() else []
        return self.key_at(rows[0].row()) if rows else None

    def select_key(self, key: Any) -> None:
        for r in range(self.rowCount()):
            if self.key_at(r) == key:
                self.selectRow(r)
                return

    def as_lists(self) -> tuple[list[str], list[list[str]]]:
        rows = [[(self.item(r, c).text() if self.item(r, c) else "")
                 for c in range(self.columnCount())] for r in range(self.rowCount())]
        return self.columns, rows

    def export_csv(self, parent: QWidget, default_name: str) -> None:
        path, _ = QFileDialog.getSaveFileName(parent, "Export CSV", default_name, "CSV files (*.csv)")
        if not path:
            return
        headers, rows = self.as_lists()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)

    def to_html(self, title: str, subtitle: str) -> str:
        from ..core.receipts import table_html
        headers, rows = self.as_lists()
        bold = {r for r in range(self.rowCount())
                if self.item(r, 0) and self.item(r, 0).font().bold()}
        return table_html(title, subtitle, headers, rows, self.right_align, bold)


# ---- printing -----------------------------------------------------------------


def _document(html: str) -> QTextDocument:
    doc = QTextDocument()
    doc.setHtml(html)
    return doc


def print_preview(parent: QWidget, html: str) -> None:
    printer = QPrinter(QPrinter.HighResolution)
    dialog = QPrintPreviewDialog(printer, parent)
    dialog.paintRequested.connect(lambda p: _document(html).print_(p))
    dialog.resize(900, 700)
    dialog.exec()


def print_direct(parent: QWidget, html: str) -> None:
    printer = QPrinter(QPrinter.HighResolution)
    if QPrintDialog(printer, parent).exec():
        _document(html).print_(printer)


def save_pdf(parent: QWidget, html: str, default_name: str) -> None:
    path, _ = QFileDialog.getSaveFileName(parent, "Save PDF", default_name, "PDF files (*.pdf)")
    if not path:
        return
    if not path.lower().endswith(".pdf"):
        path += ".pdf"
    printer = QPrinter(QPrinter.HighResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(path)
    _document(html).print_(printer)


def export_buttons(parent: QWidget, table: DataTable, title: Callable[[], str],
                   subtitle: Callable[[], str]) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addStretch(1)
    for text, fn in (
        ("Export CSV…", lambda: table.export_csv(parent, f"{title()}.csv")),
        ("Print…", lambda: print_preview(parent, table.to_html(title(), subtitle()))),
        ("Save PDF…", lambda: save_pdf(parent, table.to_html(title(), subtitle()), f"{title()}.pdf")),
    ):
        b = QPushButton(text)
        b.clicked.connect(fn)
        row.addWidget(b)
    return row


# ---- layout helpers -----------------------------------------------------------


def heading(text: str, size: int = 16) -> QLabel:
    label = QLabel(text)
    f = label.font()
    f.setPointSize(size)
    f.setBold(True)
    label.setFont(f)
    return label


def muted(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(True)
    return label


class KpiCard(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        self.title = muted(title)
        self.value = QLabel("–")
        f = QFont(self.value.font())
        f.setPointSize(17)
        f.setBold(True)
        self.value.setFont(f)
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail = muted("")
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)

    def set(self, value: str, detail: str = "") -> None:
        self.value.setText(value)
        # Keep the line even when empty so all cards share one height.
        self.detail.setText(detail or " ")


class Page(QWidget):
    """A top-level screen. `refresh` reloads data from the database."""

    title = ""
    permission = ""  # needed to see this page

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._dirty = True

    @property
    def db(self) -> Database:
        # Always the currently open database (it changes after a restore).
        return self.ctx.db

    def refresh(self) -> None:  # pragma: no cover - overridden
        pass

    def mark_dirty(self) -> None:
        self._dirty = True
        if self.isVisible():
            self.refresh_if_dirty()

    def refresh_if_dirty(self) -> None:
        if self._dirty:
            self._dirty = False
            self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_if_dirty()


def page_layout(page: QWidget, title: str, subtitle: str = "") -> QVBoxLayout:
    layout = QVBoxLayout(page)
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(10)
    layout.addWidget(heading(title))
    if subtitle:
        layout.addWidget(muted(subtitle))
    return layout


def button(text: str, slot: Callable | None = None, primary: bool = False,
           tooltip: str = "") -> QPushButton:
    b = QPushButton(text)
    if primary:
        b.setObjectName("primary")
        b.setDefault(False)
    if slot:
        b.clicked.connect(slot)
    if tooltip:
        b.setToolTip(tooltip)
    return b


def bps_text(bps: int) -> str:
    from ..core.money import format_bps
    return format_bps(bps)
