"""
ZenGrid — Technical Indicators.

Pure computation functions implemented from scratch using pandas. No external
TA-library dependencies. All functions return pd.Series/values aligned with the
input index. Only the indicators the Dark-Venus strategy needs are provided:
RSI, EMA, ATR, SMA, and Bollinger Bands.
"""

from typing import Tuple

import numpy as np
import pandas as pd


def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI (Relative Strength Index) using Wilder's smoothing.

    Args:
        closes: Series of closing prices.
        period: RSI lookback period (default 14).

    Returns:
        Series of RSI values (0–100). Initial values will be NaN.
    """
    delta = closes.diff()
    gains = delta.where(delta > 0, 0.0)
    losses = (-delta).where(delta < 0, 0.0)

    # Wilder's smoothing (EMA with alpha = 1/period)
    avg_gain = gains.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def compute_ema(closes: pd.Series, period: int = 200) -> pd.Series:
    """Calculate Exponential Moving Average.

    Args:
        closes: Series of closing prices.
        period: EMA span period (default 200).

    Returns:
        Series of EMA values. Initial values will be NaN.
    """
    return closes.ewm(span=period, adjust=False, min_periods=period).mean()


def compute_sma(closes: pd.Series, period: int = 20) -> pd.Series:
    """Calculate Simple Moving Average.

    Args:
        closes: Series of closing prices.
        period: SMA window (default 20).

    Returns:
        Series of SMA values. Initial values will be NaN.
    """
    return closes.rolling(window=period, min_periods=period).mean()


def compute_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Calculate Average True Range using Wilder's smoothing.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of closing prices.
        period: ATR lookback period (default 14).

    Returns:
        Series of ATR values. Initial values will be NaN.
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr


def compute_bollinger_bands(
    closes: pd.Series, period: int = 20, num_std: float = 2.0
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate Bollinger Bands (middle SMA, upper, lower).

    Args:
        closes: Series of closing prices.
        period: Moving-average window (default 20).
        num_std: Standard-deviation multiplier for the bands (default 2.0).

    Returns:
        Tuple of (middle, upper, lower) Series. Initial values will be NaN.
    """
    middle = closes.rolling(window=period, min_periods=period).mean()
    std = closes.rolling(window=period, min_periods=period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return middle, upper, lower
