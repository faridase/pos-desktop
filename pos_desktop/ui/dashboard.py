"""Overview of today, this month and things that need attention."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from ..core import reports
from .common import AppContext, DataTable, KpiCard, Page, heading, page_layout


class BarChart(QWidget):
    """Minimal daily sales bar chart that follows the palette (light/dark)."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.data: list[tuple[str, int]] = []
        self.setMinimumHeight(170)

    def set_data(self, data: list[tuple[str, int]]) -> None:
        self.data = data
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        text = pal.color(QPalette.Text)
        muted = QColor(text)
        muted.setAlphaF(0.55)
        bar = pal.color(QPalette.Highlight)
        w, h = self.width(), self.height()
        top, bottom, left = 18, 22, 6
        if not self.data:
            p.setPen(muted)
            p.drawText(self.rect(), Qt.AlignCenter, "No sales in the last 14 days")
            return
        peak = max(v for _, v in self.data) or 1
        slot = (w - left * 2) / len(self.data)
        bar_w = max(4.0, slot * 0.62)
        for i, (label, value) in enumerate(self.data):
            x = left + i * slot + (slot - bar_w) / 2
            bh = (h - top - bottom) * value / peak
            p.setPen(Qt.NoPen)
            p.setBrush(bar)
            p.drawRoundedRect(QRectF(x, h - bottom - bh, bar_w, bh), 3, 3)
            p.setPen(muted)
            p.drawText(QRectF(x - 10, h - bottom + 3, bar_w + 20, bottom),
                       Qt.AlignHCenter | Qt.AlignTop, label)
            if value == peak:
                p.setPen(text)
                p.drawText(QRectF(x - 40, h - bottom - bh - top, bar_w + 80, top),
                           Qt.AlignHCenter | Qt.AlignBottom, self.ctx.money(value))


class DashboardPage(Page):
    title = "Dashboard"
    permission = "dashboard"

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        layout = page_layout(self, "Dashboard")
        grid = QGridLayout()
        grid.setSpacing(10)
        names = [
            ("today", "Today's sales"), ("today_count", "Transactions today"),
            ("month", "Net sales this month"), ("profit", "Gross profit this month"),
            ("net_income", "Net profit this month"), ("cash", "Cash on hand"),
            ("bank", "Bank + card clearing"), ("inventory", "Inventory at cost"),
            ("ar", "Customers owe you"), ("ap", "You owe suppliers"),
        ]
        self.cards: dict[str, KpiCard] = {}
        for i, (key, title) in enumerate(names):
            card = KpiCard(title)
            self.cards[key] = card
            grid.addWidget(card, i // 5, i % 5)
        layout.addLayout(grid)

        layout.addWidget(heading("Sales, last 14 days", 12))
        self.chart = BarChart(ctx)
        layout.addWidget(self.chart)

        row = QHBoxLayout()
        for attr, title, cols, right in (
            ("top", "Top products (30 days)", ["Product", "Qty", "Net sales"], (1, 2)),
            ("low", "Low stock", ["Product", "On hand", "Reorder at"], (1, 2)),
            ("recent", "Recent sales", ["Receipt", "Time", "Customer", "Total"], (3,)),
        ):
            box = QVBoxLayout()
            box.addWidget(heading(title, 12))
            table = DataTable(cols, right_align=right, stretch=0, sortable=False)
            setattr(self, attr, table)
            box.addWidget(table)
            row.addLayout(box, 1)
        layout.addLayout(row, 1)

    def refresh(self) -> None:
        d = reports.dashboard(self.db)
        m = self.ctx.money
        self.cards["today"].set(m(d["today"]["total"]),
                                f"{m(d['today']['refunds'])} refunded" if d["today"]["refunds"] else "")
        self.cards["today_count"].set(str(d["today"]["count"]),
                                      f"Average {m(d['today']['average'])}" if d["today"]["count"] else "")
        self.cards["month"].set(m(d["month"]["net_sales"]), f"{d['month']['count']} sales")
        self.cards["profit"].set(m(d["month"]["gross_profit"]), "Net sales − cost of goods")
        self.cards["net_income"].set(m(d["month_net_income"]), "After expenses")
        self.cards["cash"].set(m(d["cash"]))
        self.cards["bank"].set(m(d["bank"]))
        self.cards["inventory"].set(m(d["inventory"]))
        self.cards["ar"].set(m(d["receivable"]))
        self.cards["ap"].set(m(d["payable"]), f"Tax payable {m(d['tax_payable'])}")
        self.chart.set_data([(r["date"][8:10], r["net_total"]) for r in d["daily"]])
        self.top.set_rows([[r["name"], str(r["quantity"]), m(r["net"])] for r in d["top_products"]])
        self.low.set_rows([[r["name"], str(r["stock"]), str(r["reorder_level"])]
                           for r in d["low_stock"]])
        self.recent.set_rows([[f"#{r['receipt_no']:05d}", r["created_at"][5:16],
                               r["customer"] or "", m(r["total"])] for r in d["recent"]])
