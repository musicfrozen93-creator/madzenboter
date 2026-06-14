"""
Zentry Futures Core — Exchange Utility Functions.

Precision rounding, min-notional validation, and margin calculations.
These are pure functions with no side effects.
"""

import math
from typing import Any


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


def round_quantity(qty: float, market_info: dict) -> float:
    """Round a quantity DOWN to the exchange's lot-size precision.

    Always rounds down (floor) to avoid exceeding available margin.

    Args:
        qty: Raw quantity value.
        market_info: CCXT market dict.

    Returns:
        Quantity floored to the correct step size.
    """
    precision = market_info.get('precision', {}).get('amount', 8)
    if isinstance(precision, float) and precision > 0:
        # Step-size format
        decimals = max(0, -int(math.floor(math.log10(precision))))
        factor = 10 ** decimals
        return math.floor(qty * factor) / factor
    # Decimal-count format
    decimals = int(precision)
    factor = 10 ** decimals
    return math.floor(qty * factor) / factor


def validate_min_notional(qty: float, price: float, market_info: dict) -> bool:
    """Check if order notional meets the exchange minimum.

    Args:
        qty: Order quantity.
        price: Current price.
        market_info: CCXT market dict.

    Returns:
        True if qty * price >= min notional, False otherwise.
    """
    min_notional = (
        market_info.get('limits', {}).get('cost', {}).get('min')
        or 5.0  # Binance default
    )
    notional = qty * price
    return notional >= min_notional


def calculate_margin_required(qty: float, price: float, leverage: int) -> float:
    """Calculate margin required for a position.

    Args:
        qty: Order quantity.
        price: Entry price.
        leverage: Leverage multiplier.

    Returns:
        Required margin in USDT.
    """
    if leverage <= 0:
        leverage = 1
    return (qty * price) / leverage


def calculate_notional(qty: float, price: float) -> float:
    """Calculate position notional value.

    Args:
        qty: Position quantity.
        price: Current price.

    Returns:
        Notional value in USDT.
    """
    return qty * price
