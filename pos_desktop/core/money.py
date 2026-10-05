"""Money helpers.

All amounts are integers in the currency's minor unit (cents for USD, whole
rupiah for IDR with 0 decimals). Never use floats for money.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def round_div(numerator: int, denominator: int) -> int:
    """Integer division rounding half away from zero."""
    if denominator == 0:
        raise ZeroDivisionError
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    sign = -1 if numerator < 0 else 1
    return sign * ((abs(numerator) * 2 + denominator) // (2 * denominator))


def percent_of(amount: int, bps: int) -> int:
    """`bps` is basis points: 1000 = 10.00%."""
    return round_div(amount * bps, 10000)


def allocate(total: int, weights: list[int]) -> list[int]:
    """Split `total` across `weights` proportionally so the parts sum exactly
    to `total` (largest remainder method)."""
    if not weights:
        return []
    weight_sum = sum(weights)
    if weight_sum <= 0:
        parts = [0] * len(weights)
        parts[0] = total
        return parts
    raw = [total * w for w in weights]
    parts = [r // weight_sum for r in raw]
    remainders = sorted(
        range(len(weights)), key=lambda i: raw[i] % weight_sum, reverse=True
    )
    for i in remainders[: total - sum(parts)]:
        parts[i] += 1
    return parts


def format_bps(bps: int) -> str:
    value = Decimal(bps) / 100
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def parse_bps(text: str) -> int | None:
    try:
        value = Decimal(text.strip().rstrip("%").strip() or "x")
    except InvalidOperation:
        return None
    if value < 0 or value > 100:
        return None
    return int((value * 100).to_integral_value())


@dataclass(frozen=True)
class MoneyFormat:
    symbol: str = "$"
    decimals: int = 2
    thousands_sep: str = ","
    decimal_sep: str = "."

    def format(self, amount: int, symbol: bool = True) -> str:
        negative = amount < 0
        scale = 10**self.decimals
        whole, frac = divmod(abs(amount), scale)
        whole_text = f"{whole:,}".replace(",", self.thousands_sep)
        text = whole_text
        if self.decimals:
            text += self.decimal_sep + str(frac).zfill(self.decimals)
        if symbol and self.symbol:
            text = f"{self.symbol}{text}"
        return f"-{text}" if negative else text

    def to_input(self, amount: int) -> str:
        """Plain text for an editable field (no symbol or grouping)."""
        negative = amount < 0
        scale = 10**self.decimals
        whole, frac = divmod(abs(amount), scale)
        text = str(whole)
        if self.decimals:
            text += self.decimal_sep + str(frac).zfill(self.decimals)
        return f"-{text}" if negative else text

    def parse(self, text: str, allow_negative: bool = False) -> int | None:
        cleaned = text.strip()
        if self.symbol:
            cleaned = cleaned.replace(self.symbol, "")
        cleaned = cleaned.replace(" ", "")
        if self.thousands_sep:
            cleaned = cleaned.replace(self.thousands_sep, "")
        if self.decimal_sep != ".":
            cleaned = cleaned.replace(self.decimal_sep, ".")
        if not cleaned:
            return None
        try:
            value = Decimal(cleaned)
        except InvalidOperation:
            return None
        if not value.is_finite() or (value < 0 and not allow_negative):
            return None
        return int((value * 10**self.decimals).to_integral_value())
