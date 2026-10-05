"""Financial statements and sales analytics."""

from __future__ import annotations


from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QTabWidget, QVBoxLayout, QWidget

from ..core import accounting as acc
from ..core import catalog, reports
from ..core import settings as st
from ..core.money import format_bps
from ..core.sales import PAYMENT_METHODS
from .common import (
    AppContext, DataTable, DateRangeBar, Page, date_edit, export_buttons, iso, muted, page_layout,
)


class ReportTab(QWidget):
    """Controls on top, a table, and export/print buttons."""

    def __init__(self, page: "ReportsPage", name: str, columns: list[str],
                 right_align=(), stretch: int | None = 0, as_of: bool = False,
                 default_range: str = "This month") -> None:
        super().__init__()
        self.page, self.ctx, self.name = page, page.ctx, name
        v = QVBoxLayout(self)
        self.controls = QHBoxLayout()
        if as_of:
            self.as_of = date_edit()
            self.as_of.dateChanged.connect(self.refresh)
            self.controls.addWidget(QLabel("As of"))
            self.controls.addWidget(self.as_of)
            self.range = None
        else:
            self.range = DateRangeBar(default_range)
            self.range.changed.connect(self.refresh)
            self.controls.addWidget(self.range)
        self.controls.addStretch(1)
        v.addLayout(self.controls)
        self.table = DataTable(columns, right_align, stretch, sortable=False)
        v.addWidget(self.table, 1)
        self.note = muted()
        v.addWidget(self.note)
        v.addLayout(export_buttons(self, self.table, lambda: self.name, self.subtitle))

    def subtitle(self) -> str:
        store = st.get(self.ctx.db, "store_name")
        period = f"As of {iso(self.as_of)}" if self.range is None else self.range.label()
        return f"{store} · {period}"

    def refresh(self) -> None:  # pragma: no cover - overridden
        pass


class SalesAnalysisTab(ReportTab):
    GROUPS = ["Day", "Product", "Category", "Payment method", "Staff"]

    def __init__(self, page: "ReportsPage") -> None:
        super().__init__(page, "Sales analysis", [""] * 7, right_align=range(1, 7))
        self.group = QComboBox()
        self.group.addItems(self.GROUPS)
        self.group.currentIndexChanged.connect(self.refresh)
        self.controls.insertWidget(1, QLabel("  Group by"))
        self.controls.insertWidget(2, self.group)

    def _columns(self, columns: list[str], right: set[int]) -> None:
        self.table.setColumnCount(len(columns))
        self.table.columns = columns
        self.table.right_align = right
        self.table.setHorizontalHeaderLabels(columns)

    def refresh(self) -> None:
        start, end = self.range.range()
        m = self.ctx.money
        g = self.group.currentText()
        self.name = f"Sales by {g.lower()}"
        if g == "Day":
            self._columns(["Date", "Sales", "Net sales", "Tax", "Total", "Refunds", "Net total"],
                          set(range(1, 7)))
            data = reports.sales_by_day(self.ctx.db, start, end)
            rows = [[r["date"], str(r["count"]), m(r["net"]), m(r["tax"]), m(r["total"]),
                     m(r["refunds"]), m(r["net_total"])] for r in data]
            keys = ("count", "net", "tax", "total", "refunds", "net_total")
            t = {k: sum(r[k] for r in data) for k in keys}
            rows.append(["Total", str(t["count"]), m(t["net"]), m(t["tax"]), m(t["total"]),
                         m(t["refunds"]), m(t["net_total"])])
        elif g in ("Product", "Category"):
            data = (reports.sales_by_product if g == "Product" else reports.sales_by_category)(
                self.ctx.db, start, end)
            self._columns([g, "Qty", "Net sales", "Cost", "Gross profit", "Margin"],
                          {1, 2, 3, 4, 5})
            rows = [[r["name"] if g == "Product" else r["category"], str(r["quantity"]),
                     m(r["net"]), m(r["cost"]), m(r["profit"]), format_bps(r["margin_bps"])]
                    for r in data]
            net, cost = sum(r["net"] for r in data), sum(r["cost"] for r in data)
            rows.append(["Total", str(sum(r["quantity"] for r in data)), m(net), m(cost),
                         m(net - cost), format_bps((net - cost) * 10000 // net if net else 0)])
        elif g == "Staff":
            data = reports.sales_by_staff(self.ctx.db, start, end)
            self._columns(["Staff", "Sales", "Net sales", "Discounts", "Total", "Average",
                           "Approved discounts", "Refunds"], set(range(1, 8)))
            rows = [[r["name"], str(r["count"]), m(r["net"]), m(r["discount"]), m(r["total"]),
                     m(r["average"]), str(r["approvals"]),
                     f"{r['refund_count']} ({m(r['refunds'])})"] for r in data]
            rows.append(["Total", str(sum(r["count"] for r in data)),
                         m(sum(r["net"] for r in data)), m(sum(r["discount"] for r in data)),
                         m(sum(r["total"] for r in data)), "",
                         str(sum(r["approvals"] for r in data)),
                         m(sum(r["refunds"] for r in data))])
        else:
            data = reports.sales_by_payment(self.ctx.db, start, end)
            self._columns(["Method", "Payments", "Received", "Refunded", "Net"], {1, 2, 3, 4})
            rows = [[PAYMENT_METHODS.get(r["method"], r["method"]), str(r["count"]),
                     m(r["amount"]), m(r["refunds"]), m(r["net"])] for r in data]
            rows.append(["Total", str(sum(r["count"] for r in data)),
                         m(sum(r["amount"] for r in data)), m(sum(r["refunds"] for r in data)),
                         m(sum(r["net"] for r in data))])
        self.table.set_rows(rows, bold_rows={len(rows) - 1})
        s = reports.sales_summary(self.ctx.db, start, end)
        self.note.setText(
            f"{s['count']} sales · average {m(s['average'])} · discounts {m(s['discount'])} · "
            f"{s['refund_count']} refunds ({m(s['refunds'])}) · gross profit {m(s['gross_profit'])}")


class ProfitLossTab(ReportTab):
    def __init__(self, page: "ReportsPage") -> None:
        super().__init__(page, "Profit and Loss", ["Account", "Amount"], right_align=(1,))

    def refresh(self) -> None:
        start, end = self.range.range()
        pl = acc.profit_and_loss(self.ctx.db, start, end)
        m = self.ctx.money
        rows, bold = [], set()

        def section(sec, total_label: str) -> None:
            bold.add(len(rows))
            rows.append([sec.title, ""])
            for r in sec.rows:
                rows.append([f"    {r.code}  {r.name}", m(r.amount)])
            bold.add(len(rows))
            rows.append([total_label, m(sec.total)])
            rows.append(["", ""])

        section(pl["income"], "Total revenue")
        section(pl["cogs"], "Total cost of sales")
        bold.add(len(rows))
        rows.append(["GROSS PROFIT", m(pl["gross_profit"])])
        rows.append(["", ""])
        section(pl["expenses"], "Total operating expenses")
        bold.add(len(rows))
        rows.append(["NET PROFIT" if pl["net_income"] >= 0 else "NET LOSS", m(pl["net_income"])])
        self.table.set_rows(rows, bold_rows=bold)
        income = pl["income"].total
        self.note.setText(
            f"Gross margin {format_bps(pl['gross_profit'] * 10000 // income)} · "
            f"net margin {format_bps(pl['net_income'] * 10000 // income)}" if income else "")


class BalanceSheetTab(ReportTab):
    def __init__(self, page: "ReportsPage") -> None:
        super().__init__(page, "Balance Sheet", ["Account", "Amount"], right_align=(1,), as_of=True)

    def refresh(self) -> None:
        bs = acc.balance_sheet(self.ctx.db, iso(self.as_of))
        m = self.ctx.money
        rows, bold = [], set()
        for key, total_label in (("assets", "Total assets"), ("liabilities", "Total liabilities"),
                                 ("equity", "Total equity")):
            sec = bs[key]
            bold.add(len(rows))
            rows.append([sec.title, ""])
            for r in sec.rows:
                rows.append([f"    {r.code}  {r.name}".rstrip(), m(r.amount)])
            bold.add(len(rows))
            rows.append([total_label, m(sec.total)])
            rows.append(["", ""])
        bold.add(len(rows))
        rows.append(["Total liabilities and equity",
                     m(bs["liabilities"].total + bs["equity"].total)])
        self.table.set_rows(rows, bold_rows=bold)
        self.note.setText("✓ Balanced: assets equal liabilities plus equity." if bs["balanced"]
                          else "⚠ Out of balance. Please report this as a bug.")


class TrialBalanceTab(ReportTab):
    def __init__(self, page: "ReportsPage") -> None:
        super().__init__(page, "Trial Balance", ["Code", "Account", "Type", "Debit", "Credit"],
                         right_align=(3, 4), stretch=1, as_of=True)

    def refresh(self) -> None:
        tb = acc.trial_balance(self.ctx.db, iso(self.as_of))
        m = self.ctx.money
        rows = [[r["account"].code, r["account"].name, acc.TYPE_LABELS[r["account"].type],
                 m(r["debit"]) if r["debit"] else "", m(r["credit"]) if r["credit"] else ""]
                for r in tb]
        d, c = sum(r["debit"] for r in tb), sum(r["credit"] for r in tb)
        rows.append(["", "Total", "", m(d), m(c)])
        self.table.set_rows(rows, bold_rows={len(rows) - 1})
        self.note.setText("✓ Debits equal credits." if d == c else "⚠ Debits and credits differ.")


class TaxTab(ReportTab):
    def __init__(self, page: "ReportsPage") -> None:
        super().__init__(page, "Sales tax", ["Item", "Amount"], right_align=(1,))

    def refresh(self) -> None:
        start, end = self.range.range()
        t = reports.tax_report(self.ctx.db, start, end)
        m = self.ctx.money
        label = st.get(self.ctx.db, "tax_label")
        rows = [
            ["Taxable sales (net of discounts)", m(t["taxable_sales"])],
            ["Tax-exempt sales", m(t["exempt_sales"])],
            [f"{label} collected", m(t["collected"])],
            [f"{label} refunded", m(-t["refunded"])],
            [f"Net {label.lower()} for period", m(t["net"])],
            ["", ""],
            [f"{label} payable balance at {end}", m(t["liability_balance"])],
        ]
        self.table.set_rows(rows, bold_rows={4, 6})
        self.note.setText("Record a payment to the tax authority as a journal entry: "
                          "debit Sales Tax Payable, credit Bank.")


class InventoryTab(ReportTab):
    def __init__(self, page: "ReportsPage") -> None:
        super().__init__(page, "Inventory valuation",
                         ["SKU", "Product", "Category", "On hand", "Unit cost", "Value",
                          "Retail value"], right_align=(3, 4, 5, 6), stretch=1, as_of=True)
        self.as_of.setEnabled(False)
        self.as_of.setToolTip("Inventory valuation shows current stock")

    def refresh(self) -> None:
        rows = catalog.inventory_valuation(self.ctx.db)
        m = self.ctx.money
        out = [[r["sku"], r["name"], r["category"], str(r["stock"]), m(r["cost"]), m(r["value"]),
                m(r["retail_value"])] for r in rows]
        out.append(["", "Total", "", str(sum(r["stock"] for r in rows)), "",
                    m(sum(r["value"] for r in rows)), m(sum(r["retail_value"] for r in rows))])
        self.table.set_rows(out, bold_rows={len(out) - 1})


class ReportsPage(Page):
    title = "Reports"
    permission = "reports"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        layout = page_layout(self, "Reports")
        self.tabs = QTabWidget()
        self.reports = [
            (SalesAnalysisTab(self), "Sales"),
            (ProfitLossTab(self), "Profit && Loss"),
            (BalanceSheetTab(self), "Balance Sheet"),
            (TrialBalanceTab(self), "Trial Balance"),
            (TaxTab(self), "Tax"),
            (InventoryTab(self), "Inventory"),
        ]
        for tab, label in self.reports:
            self.tabs.addTab(tab, label)
        self.tabs.currentChanged.connect(lambda i: self.reports[i][0].refresh())
        layout.addWidget(self.tabs, 1)

    def refresh(self) -> None:
        self.reports[self.tabs.currentIndex()][0].refresh()
        # Other tabs refresh when selected.


