"""
ZenGrid — BTC Trend Filter.

Classifies BTC's 15m trend direction and uses it as a global trade-direction
gate that runs before every new basket:

    BULLISH → BTC price above 200 EMA AND EMA50 above EMA200 → allow LONG,  block SHORT
    BEARISH → BTC price below 200 EMA AND EMA50 below EMA200 → allow SHORT, block LONG
    NEUTRAL → no clear trend                                 → allow BOTH
    UNKNOWN → insufficient / missing data (fail-safe)        → allow BOTH

This is a PURE function (no network / no ccxt) so it is fully unit-testable.
"""

import logging

import pandas as pd

from config.settings import BtcRegime, Settings
from signals.indicators import compute_ema

logger = logging.getLogger(__name__)


def classify_btc_regime(df_15m: pd.DataFrame, settings: Settings) -> BtcRegime:
    """Classify BTC's current 15m trend regime.

    BULLISH and BEARISH both require BOTH conditions (price vs EMA200 AND the
    EMA50/EMA200 cross alignment). Anything else is NEUTRAL. Any data problem
    yields UNKNOWN (fail-safe → trading allowed in both directions).

    Args:
        df_15m: 15-minute OHLCV DataFrame for BTC (columns include 'close').
        settings: Application settings (EMA periods).

    Returns:
        A BtcRegime value.
    """
    try:
        slow = settings.btc_ema_slow
        fast = settings.btc_ema_fast
        if df_15m is None or len(df_15m) < slow:
            return BtcRegime.UNKNOWN

        ema_fast = compute_ema(df_15m['close'], period=fast).dropna()
        ema_slow = compute_ema(df_15m['close'], period=slow).dropna()
        if ema_fast.empty or ema_slow.empty:
            return BtcRegime.UNKNOWN

        price = float(df_15m['close'].iloc[-1])
        ema_fast_val = float(ema_fast.iloc[-1])
        ema_slow_val = float(ema_slow.iloc[-1])

        if pd.isna(price) or pd.isna(ema_fast_val) or pd.isna(ema_slow_val):
            return BtcRegime.UNKNOWN

        if price > ema_slow_val and ema_fast_val > ema_slow_val:
            return BtcRegime.BULLISH
        if price < ema_slow_val and ema_fast_val < ema_slow_val:
            return BtcRegime.BEARISH
        return BtcRegime.NEUTRAL

    except Exception as e:  # fail-safe — never block trading on a data error
        logger.warning('BTC regime classification failed: %s — defaulting to UNKNOWN', e)
        return BtcRegime.UNKNOWN


def regime_allows_side(regime: BtcRegime, side: str) -> bool:
    """Return True if the BTC regime permits opening a trade on ``side``.

    BULLISH → only 'long'  (SHORT blocked)
    BEARISH → only 'short' (LONG blocked)
    NEUTRAL → both
    UNKNOWN → both (fail-safe)
    """
    side = (side or '').lower()
    if regime == BtcRegime.BULLISH:
        return side == 'long'
    if regime == BtcRegime.BEARISH:
        return side == 'short'
    # NEUTRAL / UNKNOWN → allow both
    return True
