"""
ZenGrid — BTC Regime Filter.

Classifies BTC's higher-timeframe market direction into an "impulse" regime that
is used as a global trade-direction filter:

    UP_IMPULSE   → BTC in a strong uptrend   → allow LONG,  block SHORT
    DOWN_IMPULSE → BTC in a strong downtrend → allow SHORT, block LONG
    SIDEWAYS     → no strong impulse          → allow BOTH
    UNKNOWN      → insufficient/missing data  → fail-safe: allow BOTH

The classification reuses the existing EMA200 (trend direction) and ADX (trend
strength) building blocks on the 1h timeframe, so it is consistent with the rest
of the signal engine. This module is a PURE function (no network / no ccxt) so it
is fully unit-testable.
"""

import logging

import pandas as pd

from config.settings import BtcRegime, Settings
from signals.indicators import compute_adx, compute_ema

logger = logging.getLogger(__name__)


def classify_btc_regime(df_1h: pd.DataFrame, settings: Settings) -> BtcRegime:
    """Classify BTC's current regime from 1h OHLCV data.

    A regime is an IMPULSE only when the trend is strong (ADX above the trend
    threshold) AND directional (price clearly above/below EMA200). Otherwise it
    is SIDEWAYS. Any data problem yields UNKNOWN (fail-safe → allow trading).

    Args:
        df_1h: 1-hour OHLCV DataFrame for BTC (columns: high, low, close, ...).
        settings: Application settings (thresholds, EMA/ADX periods).

    Returns:
        A BtcRegime value.
    """
    try:
        if df_1h is None or len(df_1h) < settings.ema_period:
            return BtcRegime.UNKNOWN

        ema = compute_ema(df_1h['close'], period=settings.ema_period).dropna()
        adx = compute_adx(
            df_1h['high'], df_1h['low'], df_1h['close'], period=settings.adx_period
        ).dropna()

        if ema.empty or adx.empty:
            return BtcRegime.UNKNOWN

        price = float(df_1h['close'].iloc[-1])
        ema_val = float(ema.iloc[-1])
        adx_val = float(adx.iloc[-1])

        if pd.isna(price) or pd.isna(ema_val) or pd.isna(adx_val):
            return BtcRegime.UNKNOWN

        trending = adx_val > settings.adx_trend_threshold

        if trending and price > ema_val:
            return BtcRegime.UP_IMPULSE
        if trending and price < ema_val:
            return BtcRegime.DOWN_IMPULSE
        return BtcRegime.SIDEWAYS

    except Exception as e:  # fail-safe — never block trading on a data error
        logger.warning('BTC regime classification failed: %s — defaulting to UNKNOWN', e)
        return BtcRegime.UNKNOWN


def regime_allows_side(regime: BtcRegime, side: str) -> bool:
    """Return True if the BTC regime permits opening a trade on ``side``.

    UP_IMPULSE   → only 'long'
    DOWN_IMPULSE → only 'short'
    SIDEWAYS     → both
    UNKNOWN      → both (fail-safe)

    Args:
        regime: The current BtcRegime.
        side: 'long' or 'short'.

    Returns:
        True if the side is allowed under the given regime.
    """
    side = (side or '').lower()
    if regime == BtcRegime.UP_IMPULSE:
        return side == 'long'
    if regime == BtcRegime.DOWN_IMPULSE:
        return side == 'short'
    # SIDEWAYS / UNKNOWN → allow both
    return True
