"""Currency display helpers.

LLM billing is always USD — that's the storage unit for ``Cost.cost_usd``
and ``BudgetPolicy.limit_usd``. But Mike runs his life in EUR / CZK / GBP
/ JPY / whatever. This module converts between storage USD and the
``display_currency`` he set in Settings.

Why no live FX: dependencies on a 3rd-party FX feed add a network leg
to every page render. The cheaper trade-off: Mike sets
``KORPHA_USD_TO_DISPLAY_RATE`` (and optionally refreshes it
quarterly when the rate drifts) and we use that. Quarterly drift on a
$50/day cap is meaningless; if it ever matters we'll add a refresh
skill.
"""
from __future__ import annotations

from decimal import Decimal

from korpha.config import get_settings


# Common ISO codes we render symbols for. Everything else falls back to
# "<CODE> <amount>" (e.g. "PLN 47.20"). Adding a new symbol is a 2-line
# patch — but most users will recognize their own ISO code, so we don't
# over-invest here.
_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "¥",
    "INR": "₹",
    "AUD": "A$",
    "CAD": "C$",
    "CHF": "CHF ",
    "CZK": "Kč ",
    "PLN": "zł ",
    "BRL": "R$",
    "MXN": "Mex$",
}


def usd_to_display(amount_usd: Decimal | float) -> Decimal:
    """Convert a USD amount to Mike's chosen display currency."""
    s = get_settings()
    rate = Decimal(str(s.usd_to_display_rate or 1.0))
    return Decimal(str(amount_usd)) * rate


def display_to_usd(amount_display: Decimal | float) -> Decimal:
    """Inverse: Mike entered "50 CZK"; we store as USD."""
    s = get_settings()
    rate = Decimal(str(s.usd_to_display_rate or 1.0))
    if rate == 0:
        rate = Decimal("1")
    return Decimal(str(amount_display)) / rate


def format_amount(amount_usd: Decimal | float) -> str:
    """Format a USD amount for display in the user's currency."""
    s = get_settings()
    code = (s.display_currency or "USD").upper()
    display = usd_to_display(amount_usd)
    symbol = _SYMBOLS.get(code)
    # Two decimals for normal currencies; zero for yen / similar
    decimals = 0 if code in {"JPY", "CNY", "KRW"} else 2
    fmt = f"{{:,.{decimals}f}}"
    body = fmt.format(float(display))
    if symbol is None:
        return f"{code} {body}"
    return f"{symbol}{body}" if not symbol.endswith(" ") else f"{symbol}{body}"


__all__ = [
    "display_to_usd",
    "format_amount",
    "usd_to_display",
]
