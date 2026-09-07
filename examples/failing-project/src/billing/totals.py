"""Invoice arithmetic.

Everything here is done in floats, which is the bug: money and binary floating
point do not mix, and the failures only show up on particular values.

Three defensible fixes exist, and they are not equivalent:

  A. round() the result at each step        -- cheap, still binary underneath
  B. decimal.Decimal with ROUND_HALF_UP     -- correct, slightly more code
  C. truncate to cents with math.floor      -- deterministic, but loses money

The point of the example is to try all three in separate sandboxes and let
compare_experiments show which one actually passes.
"""

from __future__ import annotations


def line_total(unit_price: float, quantity: int) -> float:
    """Total for one invoice line."""
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    return unit_price * quantity


def apply_discount(amount: float, percent: float) -> float:
    """Apply a percentage discount."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return amount - (amount * percent / 100)


def split_tax(amount: float, rate: float, parts: int) -> list[float]:
    """Split the tax on an amount across `parts` payers."""
    if parts < 1:
        raise ValueError("parts must be at least 1")
    tax = amount * rate
    return [tax / parts] * parts


def invoice_total(lines: list[tuple[float, int]], discount_percent: float = 0.0) -> float:
    """Sum the lines, then apply the invoice-level discount."""
    subtotal = sum(line_total(price, quantity) for price, quantity in lines)
    return apply_discount(subtotal, discount_percent)
