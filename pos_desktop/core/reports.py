"""Sales analytics and dashboard figures. Financial statements live in
accounting.py."""

from __future__ import annotations

from datetime import date, timedelta

from . import accounting as acc
from .db import Database


def sales_summary(db: Database, start: str, end: str) -> dict:
    s = db.one(
        "SELECT COUNT(*) AS count, COALESCE(SUM(subtotal), 0) AS gross, "
        "COALESCE(SUM(discount), 0) AS discount, COALESCE(SUM(subtotal - discount), 0) AS net, "
        "COALESCE(SUM(tax), 0) AS tax, COALESCE(SUM(total), 0) AS total "
        "FROM sales WHERE date >= ? AND date <= ?", (start, end))
    cost = db.scalar(
        "SELECT SUM(i.unit_cost * i.quantity) FROM sale_items i JOIN sales s ON s.id = i.sale_id "
        "WHERE i.track_stock = 1 AND s.date >= ? AND s.date <= ?", (start, end), 0)
    r = db.one(
        "SELECT COUNT(*) AS count, COALESCE(SUM(net), 0) AS net, COALESCE(SUM(tax), 0) AS tax "
        "FROM refunds WHERE date >= ? AND date <= ?", (start, end))
    restocked_cost = db.scalar(
        "SELECT SUM(si.unit_cost * ri.quantity) FROM refund_items ri "
        "JOIN refunds r ON r.id = ri.refund_id JOIN sale_items si ON si.id = ri.sale_item_id "
        "WHERE r.restock = 1 AND si.track_stock = 1 AND r.date >= ? AND r.date <= ?",
        (start, end), 0)
    net_sales = s["net"] - r["net"]
    cogs = cost - restocked_cost
    return {
        "count": s["count"],
        "gross": s["gross"],
        "discount": s["discount"],
        "net": s["net"],
        "tax": s["tax"] - r["tax"],
        "total": s["total"],
        "refund_count": r["count"],
        "refunds": r["net"] + r["tax"],
        "net_sales": net_sales,
        "cogs": cogs,
        "gross_profit": net_sales - cogs,
        "average": s["total"] // s["count"] if s["count"] else 0,
    }


def sales_by_day(db: Database, start: str, end: str) -> list[dict]:
    rows = {r["date"]: dict(r) for r in db.all(
        "SELECT date, COUNT(*) AS count, SUM(subtotal - discount) AS net, SUM(tax) AS tax, "
        "SUM(total) AS total FROM sales WHERE date >= ? AND date <= ? GROUP BY date",
        (start, end))}
    refunds = {r["date"]: r["amount"] for r in db.all(
        "SELECT date, SUM(net + tax) AS amount FROM refunds WHERE date >= ? AND date <= ? "
        "GROUP BY date", (start, end))}
    out = []
    for d in sorted(set(rows) | set(refunds)):
        row = rows.get(d, {"date": d, "count": 0, "net": 0, "tax": 0, "total": 0})
        row["refunds"] = refunds.get(d, 0)
        row["net_total"] = row["total"] - row["refunds"]
        out.append(row)
    return out


def _item_sales(db: Database, start: str, end: str, key: str) -> list[dict]:
    rows = db.all(
        f"SELECT {key} AS k, i.name, i.sku, i.category, SUM(i.quantity) AS qty, SUM(i.net) AS net, "
        "SUM(CASE WHEN i.track_stock = 1 THEN i.unit_cost * i.quantity ELSE 0 END) AS cost, "
        "COALESCE(SUM((SELECT SUM(r.quantity) FROM refund_items r WHERE r.sale_item_id = i.id)), 0) AS rqty, "
        "COALESCE(SUM((SELECT SUM(r.net) FROM refund_items r WHERE r.sale_item_id = i.id)), 0) AS rnet, "
        "COALESCE(SUM((SELECT SUM(r.quantity) FROM refund_items r JOIN refunds f ON f.id = r.refund_id "
        "  WHERE r.sale_item_id = i.id AND f.restock = 1) * i.unit_cost * i.track_stock), 0) AS rcost "
        "FROM sale_items i JOIN sales s ON s.id = i.sale_id "
        f"WHERE s.date >= ? AND s.date <= ? GROUP BY {key} ORDER BY net DESC",
        (start, end))
    out = []
    for r in rows:
        net = r["net"] - r["rnet"]
        cost = r["cost"] - r["rcost"]
        out.append({
            "name": r["name"], "sku": r["sku"], "category": r["category"],
            "quantity": r["qty"] - r["rqty"], "net": net, "cost": cost,
            "profit": net - cost,
            "margin_bps": (net - cost) * 10000 // net if net else 0,
        })
    return out


def sales_by_product(db: Database, start: str, end: str) -> list[dict]:
    return _item_sales(db, start, end, "COALESCE(i.product_id, i.sku)")


def sales_by_category(db: Database, start: str, end: str) -> list[dict]:
    return _item_sales(db, start, end, "i.category")


def sales_by_payment(db: Database, start: str, end: str) -> list[dict]:
    received = {r["method"]: dict(r) for r in db.all(
        "SELECT p.method, COUNT(*) AS count, SUM(p.amount) AS amount FROM sale_payments p "
        "JOIN sales s ON s.id = p.sale_id WHERE s.date >= ? AND s.date <= ? GROUP BY p.method",
        (start, end))}
    refunded = {r["method"]: r["amount"] for r in db.all(
        "SELECT method, SUM(net + tax) AS amount FROM refunds WHERE date >= ? AND date <= ? "
        "GROUP BY method", (start, end))}
    out = []
    for m in sorted(set(received) | set(refunded)):
        row = received.get(m, {"method": m, "count": 0, "amount": 0})
        row["refunds"] = refunded.get(m, 0)
        row["net"] = row["amount"] - row["refunds"]
        out.append(row)
    return out


def sales_by_staff(db: Database, start: str, end: str) -> list[dict]:
    rows = [dict(r) for r in db.all(
        "SELECT COALESCE(u.full_name, '(before staff logins)') AS name, COUNT(*) AS count, "
        "SUM(s.subtotal - s.discount) AS net, SUM(s.discount) AS discount, SUM(s.total) AS total, "
        "SUM(CASE WHEN s.approved_by IS NOT NULL THEN 1 ELSE 0 END) AS approvals "
        "FROM sales s LEFT JOIN users u ON u.id = s.user_id "
        "WHERE s.date >= ? AND s.date <= ? GROUP BY s.user_id ORDER BY total DESC",
        (start, end))]
    refunds = {r["name"]: (r["count"], r["amount"]) for r in db.all(
        "SELECT COALESCE(u.full_name, '(before staff logins)') AS name, COUNT(*) AS count, "
        "SUM(r.net + r.tax) AS amount FROM refunds r LEFT JOIN users u ON u.id = r.user_id "
        "WHERE r.date >= ? AND r.date <= ? GROUP BY r.user_id", (start, end))}
    for r in rows:
        r["refund_count"], r["refunds"] = refunds.pop(r["name"], (0, 0))
        r["average"] = r["total"] // r["count"] if r["count"] else 0
    for name, (count, amount) in refunds.items():
        rows.append({"name": name, "count": 0, "net": 0, "discount": 0, "total": 0,
                     "approvals": 0, "refund_count": count, "refunds": amount, "average": 0})
    return rows


def tax_report(db: Database, start: str, end: str) -> dict:
    collected = db.scalar("SELECT SUM(tax) FROM sales WHERE date >= ? AND date <= ?", (start, end), 0)
    taxable = db.scalar(
        "SELECT SUM(i.net) FROM sale_items i JOIN sales s ON s.id = i.sale_id "
        "WHERE i.tax > 0 AND s.date >= ? AND s.date <= ?", (start, end), 0)
    exempt = db.scalar(
        "SELECT SUM(i.net) FROM sale_items i JOIN sales s ON s.id = i.sale_id "
        "WHERE i.tax = 0 AND s.date >= ? AND s.date <= ?", (start, end), 0)
    refunded = db.scalar("SELECT SUM(tax) FROM refunds WHERE date >= ? AND date <= ?",
                         (start, end), 0)
    return {
        "taxable_sales": taxable,
        "exempt_sales": exempt,
        "collected": collected,
        "refunded": refunded,
        "net": collected - refunded,
        "liability_balance": acc.account_balance(db, acc.account_id(db, "tax"), end),
    }


def dashboard(db: Database, today: date | None = None) -> dict:
    today = today or date.today()
    t = today.isoformat()
    month_start = today.replace(day=1).isoformat()
    thirty = (today - timedelta(days=29)).isoformat()
    bal = acc.balances(db, t)
    pl = acc.profit_and_loss(db, month_start, t)
    return {
        "today": sales_summary(db, t, t),
        "month": sales_summary(db, month_start, t),
        "month_net_income": pl["net_income"],
        "cash": bal[acc.account_id(db, "cash")],
        "bank": bal[acc.account_id(db, "bank")] + bal[acc.account_id(db, "card")],
        "receivable": bal[acc.account_id(db, "ar")],
        "payable": bal[acc.account_id(db, "ap")],
        "inventory": bal[acc.account_id(db, "inventory")],
        "tax_payable": bal[acc.account_id(db, "tax")],
        "top_products": sales_by_product(db, thirty, t)[:8],
        "daily": sales_by_day(db, (today - timedelta(days=13)).isoformat(), t),
        "low_stock": [dict(r) for r in db.all(
            "SELECT name, sku, stock, reorder_level FROM products WHERE is_active = 1 "
            "AND track_stock = 1 AND stock <= reorder_level ORDER BY stock, name LIMIT 20")],
        "recent": [dict(r) for r in db.all(
            "SELECT s.id, s.receipt_no, s.created_at, s.total, s.status, c.name AS customer "
            "FROM sales s LEFT JOIN customers c ON c.id = s.customer_id ORDER BY s.id DESC LIMIT 10")],
    }
