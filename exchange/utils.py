"""
Zentry — Exchange Utility Functions.

Precision helpers used to render analysis output (entry, stop loss, take-profit
levels) at the exchange's real tick size. Pure functions with no side effects.

Order-sizing helpers (lot-size rounding, min-notional validation, margin and
notional calculation) lived here only to build and validate orders for automatic
execution and were removed in the Phase 0 conversion.
"""

import math


def round_price(price: float, market_info: dict) -> float:
    """Round a price to the exchange's tick-size precision.

    Args:
        price: Raw price value.
        market_info: CCXT market dict (from exchange.market(symbol)).

    Returns:
        Price rounded to the correct number of decimal places.
    """
    precision = market_info.get('precision', {}).get('price', 8)
    if isinstance(precision, float):
        # Tick-size format (e.g. 0.01)
        if precision > 0:
            decimals = max(0, -int(math.floor(math.log10(precision))))
            return round(price, decimals)
        return price
    # Decimal-count format (e.g. 2)
    return round(price, int(precision))
