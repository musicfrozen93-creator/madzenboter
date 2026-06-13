"""
Zentry Futures Core — Technical Indicators.

Pure computation functions implemented from scratch using pandas.
No external TA library dependencies. All functions return pd.Series
aligned with the input index.
"""

import pandas as pd
import numpy as np


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


def compute_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Calculate Average Directional Index for market regime detection.

    Full implementation of Wilder's ADX:
    1. +DM / -DM directional movement
    2. Smoothed +DI / -DI directional indicators
    3. DX directional index
    4. ADX smoothed average of DX

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of closing prices.
        period: ADX lookback period (default 14).

    Returns:
        Series of ADX values (0–100). Initial values will be NaN.
    """
    # Directional Movement
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(0.0, index=high.index)
    minus_dm = pd.Series(0.0, index=high.index)

    # +DM: up_move > down_move and up_move > 0
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=high.index)

    # -DM: down_move > up_move and down_move > 0
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    minus_dm = pd.Series(minus_dm, index=high.index)

    # True Range
    atr = compute_atr(high, low, close, period)

    # Smoothed +DM and -DM using Wilder's method
    smooth_plus_dm = plus_dm.ewm(
        alpha=1.0 / period, min_periods=period, adjust=False
    ).mean()
    smooth_minus_dm = minus_dm.ewm(
        alpha=1.0 / period, min_periods=period, adjust=False
    ).mean()

    # Directional Indicators
    plus_di = 100.0 * (smooth_plus_dm / atr.replace(0, np.nan))
    minus_di = 100.0 * (smooth_minus_dm / atr.replace(0, np.nan))

    # Directional Index
    di_sum = plus_di + minus_di
    di_diff = (plus_di - minus_di).abs()
    dx = 100.0 * (di_diff / di_sum.replace(0, np.nan))

    # Average Directional Index
    adx = dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    return adx
