"""Key/value store settings. Unknown keys fall back to DEFAULTS, so settings
added in later versions work with older data files."""

from __future__ import annotations

from .db import Database
from .money import MoneyFormat

DEFAULTS: dict[str, str] = {
    "store_name": "My Store",
    "store_address": "",
    "store_phone": "",
    "tax_id": "",
    "currency_symbol": "$",
    "currency_decimals": "2",
    "thousands_sep": ",",
    "decimal_sep": ".",
    "tax_rate_bps": "1000",
    "tax_label": "Tax",
    "receipt_footer": "Thank you for your purchase!",
    "allow_negative_stock": "0",
    "auto_lock_minutes": "10",
}


def get(db: Database, key: str) -> str:
    value = db.scalar("SELECT value FROM settings WHERE key = ?", (key,))
    return DEFAULTS.get(key, "") if value is None else str(value)


def get_int(db: Database, key: str) -> int:
    try:
        return int(get(db, key))
    except ValueError:
        return int(DEFAULTS.get(key, "0") or 0)


def get_bool(db: Database, key: str) -> bool:
    return get(db, key) in ("1", "true", "yes")


def get_all(db: Database) -> dict[str, str]:
    values = dict(DEFAULTS)
    for row in db.all("SELECT key, value FROM settings"):
        values[row["key"]] = row["value"]
    return values


def set_many(db: Database, values: dict[str, str]) -> None:
    with db.transaction():
        for key, value in values.items():
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


def money_format(db: Database) -> MoneyFormat:
    s = get_all(db)
    try:
        decimals = max(0, min(4, int(s["currency_decimals"])))
    except ValueError:
        decimals = 2
    return MoneyFormat(
        symbol=s["currency_symbol"],
        decimals=decimals,
        thousands_sep=s["thousands_sep"],
        decimal_sep=s["decimal_sep"] or ".",
    )


def can_change_currency_decimals(db: Database) -> bool:
    """Changing decimals would reinterpret stored amounts, so only allow it
    before any money has been recorded."""
    return (
        db.scalar("SELECT COUNT(*) FROM journal_entries", default=0) == 0
        and db.scalar(
            "SELECT COUNT(*) FROM products WHERE price <> 0 OR cost <> 0",
            default=0,
        )
        == 0
    )
