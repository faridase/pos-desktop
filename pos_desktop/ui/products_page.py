"""Product catalog management."""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QSpinBox, QWidget,
)

from ..core import catalog
from ..core.catalog import Product
from .common import (
    AppContext, DataTable, MoneyEdit, Page, button, error, info, muted, page_layout,
    run_action,
)


class ProductsPage(Page):
    title = "Products"
    permission = "products"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        layout = page_layout(self, "Products")
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, SKU or barcode")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.category = QComboBox()
        self.category.currentIndexChanged.connect(self.refresh)
        self.inactive = QCheckBox("Show inactive")
        self.inactive.toggled.connect(self.refresh)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.category)
        bar.addWidget(self.inactive)
        layout.addLayout(bar)

        self.table = DataTable(["SKU", "Barcode", "Name", "Category", "Price", "Cost", "Margin",
                                "Stock", "Reorder at", "Taxable", "Status"],
                               right_align=(4, 5, 6, 7, 8), stretch=2)
        self.table.activated_key.connect(lambda _: self._edit())
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.addWidget(button("Add product…", self._add, primary=True))
        row.addWidget(button("Edit…", self._edit))
        row.addWidget(button("Activate / deactivate", self._toggle))
        row.addStretch(1)
        self.count = muted()
        row.addWidget(self.count)
        row.addWidget(button("Import CSV…", self._import))
        row.addWidget(button("Export CSV…", self._export))
        layout.addLayout(row)

    def refresh(self) -> None:
        current = self.category.currentData()
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("All categories", None)
        for c in catalog.categories(self.db):
            self.category.addItem(c, c)
        self.category.setCurrentIndex(max(0, self.category.findData(current)))
        self.category.blockSignals(False)

        products = catalog.list_products(self.db, self.search.text().strip(),
                                         self.category.currentData(), self.inactive.isChecked())
        m = self.ctx.money
        rows, colors = [], {}
        for n, p in enumerate(products):
            margin = (p.price - p.cost) * 10000 // p.price if p.price else 0
            rows.append([
                p.sku, p.barcode, p.name, p.category, (m(p.price), p.price), (m(p.cost), p.cost),
                (f"{margin / 100:.1f}%", margin),
                (str(p.stock) if p.track_stock else "—", p.stock if p.track_stock else 10**9),
                str(p.reorder_level) if p.track_stock else "", "Yes" if p.taxable else "No",
                "Active" if p.is_active else "Inactive",
            ])
            if not p.is_active:
                colors[n] = QColor("gray")
            elif p.is_low:
                colors[n] = QColor("#c0392b")
        self.table.set_rows(rows, [p.id for p in products], colors=colors)
        self.count.setText(f"{len(products)} products")

    def _add(self) -> None:
        if ProductDialog(self.ctx, None, self).exec() == QDialog.Accepted:
            self.ctx.notify()

    def _edit(self) -> None:
        pid = self.table.selected_key()
        if pid and ProductDialog(self.ctx, catalog.get_product(self.db, pid), self).exec():
            self.ctx.notify()

    def _toggle(self) -> None:
        pid = self.table.selected_key()
        if not pid:
            return
        p = catalog.get_product(self.db, pid)
        p.is_active = not p.is_active
        run_action(self, lambda: catalog.update_product(self.db, p))
        self.ctx.notify()

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export products", "products.csv",
                                              "CSV files (*.csv)")
        if path:
            n = catalog.export_products_csv(self.db, path, self.ctx.fmt)
            info(self, f"Exported {n} products.")

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import products", "", "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            created, updated, errors = catalog.import_products_csv(self.db, path, self.ctx.fmt)
        except (OSError, UnicodeDecodeError) as e:
            error(self, f"Could not read the file: {e}")
            return
        msg = f"Created {created} and updated {updated} products."
        if errors:
            msg += "\n\nSkipped rows:\n" + "\n".join(errors[:20])
        info(self, msg, "Import finished")
        self.ctx.notify()


class ProductDialog(QDialog):
    def __init__(self, ctx: AppContext, product: Product | None, parent: QWidget) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.product = product or Product(sku=catalog.next_sku(ctx.db))
        self.setWindowTitle("Edit product" if product else "Add product")
        self.setMinimumWidth(420)
        p = self.product
        form = QFormLayout(self)
        self.name = QLineEdit(p.name)
        self.sku = QLineEdit(p.sku)
        self.barcode = QLineEdit(p.barcode)
        self.barcode.setPlaceholderText("Scan or type (optional)")
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItems(catalog.categories(ctx.db) or ["General"])
        self.category.setCurrentText(p.category)
        self.price = MoneyEdit(ctx, p.price)
        self.cost = MoneyEdit(ctx, p.cost)
        self.taxable = QCheckBox("Charge tax on this product")
        self.taxable.setChecked(p.taxable)
        self.track = QCheckBox("Track stock")
        self.track.setChecked(p.track_stock)
        self.reorder = QSpinBox()
        self.reorder.setRange(0, 1_000_000)
        self.reorder.setValue(p.reorder_level)
        self.opening = QSpinBox()
        self.opening.setRange(0, 1_000_000)
        self.active = QCheckBox("Active (available for sale)")
        self.active.setChecked(p.is_active)

        form.addRow("Name", self.name)
        form.addRow("SKU", self.sku)
        form.addRow("Barcode", self.barcode)
        form.addRow("Category", self.category)
        form.addRow("Selling price", self.price)
        form.addRow("Unit cost", self.cost)
        form.addRow("", self.taxable)
        form.addRow("", self.track)
        form.addRow("Reorder at", self.reorder)
        if product is None:
            form.addRow("Opening stock", self.opening)
            form.addRow("", muted("Opening stock is valued at unit cost and recorded "
                                  "against Opening Balance Equity."))
        else:
            form.addRow("In stock", QLabel(str(p.stock) + "  (change via Inventory)"))
        form.addRow("", self.active)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.track.toggled.connect(self.reorder.setEnabled)
        self.track.toggled.connect(self.opening.setEnabled)
        self.reorder.setEnabled(p.track_stock)
        self.opening.setEnabled(p.track_stock)

    def _save(self) -> None:
        price, cost = self.price.value(), self.cost.value()
        if price is None or cost is None:
            error(self, "Enter a valid price and cost")
            return
        p = self.product
        p.name, p.sku, p.barcode = self.name.text(), self.sku.text(), self.barcode.text()
        p.category = self.category.currentText()
        p.price, p.cost = price, cost
        p.taxable, p.track_stock = self.taxable.isChecked(), self.track.isChecked()
        p.reorder_level, p.is_active = self.reorder.value(), self.active.isChecked()
        if p.id is None:
            ok = run_action(self, lambda: catalog.create_product(self.ctx.db, p, self.opening.value()))
        else:
            ok = run_action(self, lambda: catalog.update_product(self.ctx.db, p) or True)
        if ok:
            self.accept()
