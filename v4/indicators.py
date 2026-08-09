"""V4 indicators not already provided by signals.indicators.

Only ADX / Directional Index (Wilder) and a Bollinger-bandwidth helper are added
here. RSI / EMA / SMA / ATR / Bollinger are reused from signals.indicators to
avoid duplicating (and drifting from) the existing, already-tested math.

All functions are pure and return pandas Series aligned to the input index, with
NaN during the warm-up period (matching the house style in signals.indicators).
"""

from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd

from signals.indicators import compute_bollinger_bands  # reused, not reimplemented


def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (EMA with alpha = 1/period), matching compute_atr/rsi."""
    return series.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def compute_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Average Directional Index with +DI / −DI (Wilder).

    Returns:
        (adx, plus_di, minus_di) as Series in the 0–100 range. Warm-up values are
        NaN. ADX measures trend STRENGTH (direction-agnostic); +DI vs −DI gives
        the direction.
    """
    high = high.astype('float64')
    low = low.astype('float64')
    close = close.astype('float64')

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0),
        index=high.index,
    )

    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    atr = _wilder(tr, period)
    # Guard against division by zero on flat/degenerate ranges.
    atr_safe = atr.replace(0.0, np.nan)

    plus_di = 100.0 * _wilder(plus_dm, period) / atr_safe
    minus_di = 100.0 * _wilder(minus_dm, period) / atr_safe

    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx = _wilder(dx, period)

    return adx, plus_di, minus_di


def bollinger_bandwidth(
    closes: pd.Series, period: int = 20, num_std: float = 2.0
) -> pd.Series:
    """Bollinger bandwidth = (upper − lower) / middle, a volatility-compression
    proxy used for squeeze detection. NaN during warm-up and where middle == 0.
    """
    middle, upper, lower = compute_bollinger_bands(closes, period=period, num_std=num_std)
    middle_safe = middle.replace(0.0, np.nan)
    return (upper - lower) / middle_safe
