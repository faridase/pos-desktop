"""Store, tax, currency and data settings."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QPlainTextEdit,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..core import auth
from ..core import settings as st
from ..core.db import SCHEMA_VERSION
from ..core.money import format_bps, parse_bps
from .common import AppContext, Page, button, error, heading, info, muted


class SettingsPage(Page):
    title = "Settings"
    permission = "settings"

    def __init__(self, ctx: AppContext, main_window) -> None:
        super().__init__(ctx)
        self.main = main_window
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.addWidget(heading("Settings"))

        store = QGroupBox("Store & receipt")
        f = QFormLayout(store)
        self.store_name, self.address = QLineEdit(), QLineEdit()
        self.phone, self.tax_id = QLineEdit(), QLineEdit()
        self.footer = QPlainTextEdit()
        self.footer.setFixedHeight(60)
        f.addRow("Store name", self.store_name)
        f.addRow("Address", self.address)
        f.addRow("Phone", self.phone)
        f.addRow("Tax / business ID", self.tax_id)
        f.addRow("Receipt footer", self.footer)
        layout.addWidget(store)

        tax = QGroupBox("Tax")
        f = QFormLayout(tax)
        self.tax_label, self.tax_rate = QLineEdit(), QLineEdit()
        self.tax_rate.setMaximumWidth(100)
        f.addRow("Tax name", self.tax_label)
        f.addRow("Rate (%)", self.tax_rate)
        f.addRow("", muted("Applies to new sales. Mark individual products as non-taxable "
                           "in Products."))
        layout.addWidget(tax)

        money = QGroupBox("Currency")
        f = QFormLayout(money)
        self.symbol = QLineEdit()
        self.symbol.setMaximumWidth(100)
        self.decimals = QSpinBox()
        self.decimals.setRange(0, 3)
        self.thousands, self.decimal = QLineEdit(), QLineEdit()
        for w in (self.thousands, self.decimal):
            w.setMaxLength(1)
            w.setMaximumWidth(50)
        self.preview = muted()
        f.addRow("Symbol", self.symbol)
        f.addRow("Decimal places", self.decimals)
        f.addRow("Thousands separator", self.thousands)
        f.addRow("Decimal separator", self.decimal)
        f.addRow("Preview", self.preview)
        self.decimals_note = muted()
        f.addRow("", self.decimals_note)
        layout.addWidget(money)

        stock = QGroupBox("Selling")
        f = QFormLayout(stock)
        self.negative = QCheckBox("Allow selling items that are out of stock")
        f.addRow(self.negative)
        layout.addWidget(stock)

        security = QGroupBox("Security")
        f = QFormLayout(security)
        self.auto_lock = QSpinBox()
        self.auto_lock.setRange(0, 240)
        self.auto_lock.setSuffix(" min")
        self.auto_lock.setSpecialValueText("Never")
        f.addRow("Lock after inactivity", self.auto_lock)
        f.addRow("", muted("The screen locks when nobody touches the keyboard or mouse for this "
                           "long. Staff sign back in to continue. Manage staff under Staff."))
        layout.addWidget(security)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button("Save settings", self._save, primary=True))
        layout.addLayout(row)

        data = QGroupBox("Data")
        v = QVBoxLayout(data)
        self.path_label = muted()
        v.addWidget(self.path_label)
        row = QHBoxLayout()
        row.addWidget(button("Back up…", main_window.backup))
        row.addWidget(button("Restore from backup…", main_window.restore))
        row.addWidget(button("Open data folder", self._open_folder))
        row.addWidget(button("Load sample data…", main_window.load_sample))
        row.addStretch(1)
        v.addLayout(row)
        layout.addWidget(data)
        layout.addStretch(1)

        for w in (self.symbol, self.thousands, self.decimal):
            w.textChanged.connect(self._preview)
        self.decimals.valueChanged.connect(self._preview)

    def refresh(self) -> None:
        s = st.get_all(self.db)
        self.store_name.setText(s["store_name"])
        self.address.setText(s["store_address"])
        self.phone.setText(s["store_phone"])
        self.tax_id.setText(s["tax_id"])
        self.footer.setPlainText(s["receipt_footer"])
        self.tax_label.setText(s["tax_label"])
        self.tax_rate.setText(format_bps(int(s["tax_rate_bps"])).rstrip("%"))
        self.symbol.setText(s["currency_symbol"])
        self.decimals.setValue(int(s["currency_decimals"]))
        self.thousands.setText(s["thousands_sep"])
        self.decimal.setText(s["decimal_sep"])
        self.negative.setChecked(s["allow_negative_stock"] == "1")
        self.auto_lock.setValue(st.get_int(self.db, "auto_lock_minutes"))
        can_change = st.can_change_currency_decimals(self.db)
        self.decimals.setEnabled(can_change)
        self.decimals_note.setText(
            "" if can_change else
            "Decimal places are locked because amounts have already been recorded.")
        self.path_label.setText(f"Data file: {self.db.path}   ·   schema v{SCHEMA_VERSION}")
        self._preview()

    def _preview(self) -> None:
        from ..core.money import MoneyFormat
        fmt = MoneyFormat(self.symbol.text(), self.decimals.value(), self.thousands.text(),
                          self.decimal.text() or ".")
        self.preview.setText(fmt.format(123456789 // (10 ** (2 - min(self.decimals.value(), 2)))))

    def _save(self) -> None:
        bps = parse_bps(self.tax_rate.text() or "0")
        if bps is None:
            error(self, "Tax rate must be between 0 and 100")
            return
        if not self.store_name.text().strip():
            error(self, "Store name is required")
            return
        if self.thousands.text() and self.thousands.text() == self.decimal.text():
            error(self, "Thousands and decimal separators must differ")
            return
        st.set_many(self.db, {
            "store_name": self.store_name.text().strip(),
            "store_address": self.address.text().strip(),
            "store_phone": self.phone.text().strip(),
            "tax_id": self.tax_id.text().strip(),
            "receipt_footer": self.footer.toPlainText().strip(),
            "tax_label": self.tax_label.text().strip() or "Tax",
            "tax_rate_bps": str(bps),
            "currency_symbol": self.symbol.text(),
            "currency_decimals": str(self.decimals.value()),
            "thousands_sep": self.thousands.text(),
            "decimal_sep": self.decimal.text() or ".",
            "allow_negative_stock": "1" if self.negative.isChecked() else "0",
            "auto_lock_minutes": str(self.auto_lock.value()),
        })
        with self.db.transaction():
            auth.log(self.db, "settings", "Settings saved")
        self.ctx.reload_settings()
        info(self, "Settings saved.")

    def _open_folder(self) -> None:
        from pathlib import Path
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.db.path).parent)))


