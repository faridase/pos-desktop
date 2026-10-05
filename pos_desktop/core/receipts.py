"""Receipt and report HTML, rendered by Qt for preview, printing and PDF."""

from __future__ import annotations

from html import escape

from . import settings as st
from .db import Database
from .sales import PAYMENT_METHODS


def receipt_html(db: Database, sale: dict) -> str:
    s = st.get_all(db)
    fmt = st.money_format(db)
    m = fmt.format

    def row(label: str, value: str, strong: bool = False) -> str:
        style = ' style="font-weight:bold; font-size:13pt"' if strong else ""
        return (f'<tr{style}><td>{escape(label)}</td>'
                f'<td align="right">{escape(value)}</td></tr>')

    header = [f'<div align="center"><b style="font-size:14pt">{escape(s["store_name"])}</b>']
    for key in ("store_address", "store_phone"):
        if s[key]:
            header.append(f"<br>{escape(s[key])}")
    if s["tax_id"]:
        header.append(f"<br>Tax ID: {escape(s['tax_id'])}")
    header.append(
        f'<br><br>Receipt #{sale["receipt_no"]:05d}<br>{escape(sale["created_at"])}')
    if sale.get("customer"):
        header.append(f"<br>Customer: {escape(sale['customer'])}")
    if sale.get("cashier"):
        header.append(f"<br>Served by: {escape(sale['cashier'])}")
    header.append("</div>")

    items = []
    for i in sale["items"]:
        items.append(
            f'<tr><td>{i["quantity"]} × {escape(i["name"])}'
            f'<br><small>@ {m(i["unit_price"])}'
            + (f' &nbsp;–{m(i["discount"])} disc.' if i["discount"] else "")
            + (' &nbsp;(no tax)' if not i["tax"] and sale["tax"] else "")
            + f'</small></td><td align="right" valign="top">{m(i["net"])}</td></tr>')

    totals = [row("Subtotal", m(sale["subtotal"]))]
    if sale["discount"]:
        totals.append(row("Discount", "-" + m(sale["discount"])))
    totals.append(row(s["tax_label"], m(sale["tax"])))
    totals.append(row("TOTAL", m(sale["total"]), strong=True))
    for p in sale["payments"]:
        label = PAYMENT_METHODS.get(p["method"], p["method"])
        amount = p["amount"] + (sale["change_given"] if p["method"] == "cash" else 0)
        totals.append(row(label, m(amount)))
    if sale["change_given"]:
        totals.append(row("Change", m(sale["change_given"])))
    if sale.get("refunded_total"):
        totals.append(row("Refunded", "-" + m(sale["refunded_total"])))

    note = f"<p>{escape(sale['note'])}</p>" if sale.get("note") else ""
    footer = f'<p align="center">{escape(s["receipt_footer"])}</p>' if s["receipt_footer"] else ""
    return (
        '<html><body style="font-family: sans-serif; font-size: 10pt">'
        + "".join(header)
        + '<hr><table width="100%" cellspacing="0" cellpadding="2">' + "".join(items) + "</table>"
        + '<hr><table width="100%" cellspacing="0" cellpadding="2">' + "".join(totals) + "</table>"
        + note + "<hr>" + footer + "</body></html>"
    )


def table_html(title: str, subtitle: str, headers: list[str], rows: list[list[str]],
               right_align: set[int] | None = None, bold_rows: set[int] | None = None) -> str:
    right_align = right_align or set()
    bold_rows = bold_rows or set()
    head = "".join(
        f'<th align="{"right" if i in right_align else "left"}">{escape(h)}</th>'
        for i, h in enumerate(headers))
    body = []
    for n, r in enumerate(rows):
        cells = "".join(
            f'<td align="{"right" if i in right_align else "left"}">'
            + (f"<b>{escape(str(c))}</b>" if n in bold_rows else escape(str(c))) + "</td>"
            for i, c in enumerate(r))
        body.append(f"<tr>{cells}</tr>")
    return (
        '<html><body style="font-family: sans-serif; font-size: 9pt">'
        f"<h2>{escape(title)}</h2><p>{escape(subtitle)}</p>"
        '<table width="100%" cellspacing="0" cellpadding="3" border="1" '
        'style="border-collapse: collapse; border-color: #999">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
        "</body></html>"
    )


