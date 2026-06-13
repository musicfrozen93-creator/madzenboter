"""
ZenGrid — BTC Regime Participation Filter (Change #3).

A GLOBAL, market-wide directional gate based on Bitcoin's trend state.
It is layered ON TOP of the existing per-symbol RSI + EMA200 entry logic
and does not modify it. It only ever *blocks counter-trend entries during a
confirmed strong BTC trend* (counter-factor protection). During ranging /
sideways BTC it is fully permissive, so it never suppresses participation in
the markets it is meant to encourage.

States (BTCRegime):
    UP_IMPULSE    — strong BTC uptrend   (ADX > trend_threshold AND price > EMA200)
    DOWN_IMPULSE  — strong BTC downtrend (ADX > trend_threshold AND price < EMA200)
    SIDEWAYS      — anything that is not a confirmed strong trend (ranging / weak)
    UNKNOWN       — data unavailable (fail OPEN → permissive)

Gate behaviour (preserves protection when trending, maximises participation
when ranging):
    UP_IMPULSE   → allow LONG, block SHORT  (no counter-trend shorts)
    DOWN_IMPULSE → allow SHORT, block LONG  (no counter-trend longs)
    SIDEWAYS     → allow BOTH (no directional penalty)
    UNKNOWN      → allow BOTH

This module reuses the existing indicators (EMA200 for direction, ADX for
trend strength) and the existing config thresholds (ema_period,
adx_trend_threshold) — it introduces no new strategy magic numbers.
"""

import logging
import time
from enum import Enum
from typing import Optional, Tuple

import pandas as pd

from config.settings import Settings
from exchange.client import ExchangeClient
from signals.indicators import compute_adx, compute_ema

logger = logging.getLogger(__name__)


class BTCRegime(str, Enum):
    """Global Bitcoin trend state used for directional participation gating."""
    UP_IMPULSE = 'up_impulse'
    DOWN_IMPULSE = 'down_impulse'
    SIDEWAYS = 'sideways'
    UNKNOWN = 'unknown'


class BTCRegimeFilter:
    """Classifies BTC trend state and gates signal direction accordingly.

    The classification is cached and only re-computed every
    ``btc_regime_refresh_seconds`` (BTC's 1h trend changes slowly, so this
    avoids an extra OHLCV fetch on every fast loop iteration).
    """

    def __init__(self, exchange_client: ExchangeClient, settings: Settings) -> None:
        self.exchange = exchange_client
        self.settings = settings
        self._regime: BTCRegime = BTCRegime.UNKNOWN
        self._last_refresh: float = 0.0

    # ───────────────────────────────────────────
    # Classification
    # ───────────────────────────────────────────

    def refresh(self, force: bool = False) -> BTCRegime:
        """Refresh the cached BTC regime if stale, returning the current state.

        Args:
            force: Re-classify even if the cache is still warm.

        Returns:
            The current BTCRegime. On any data/compute error the previous
            cached value is kept and, if none exists yet, UNKNOWN is returned
            (which the gate treats permissively — it never blocks all trading).
        """
        if not self.settings.btc_regime_enabled:
            return BTCRegime.UNKNOWN

        now = time.time()
        if not force and (now - self._last_refresh) < self.settings.btc_regime_refresh_seconds:
            return self._regime

        try:
            new_regime = self._classify()
            if new_regime != self._regime:
                logger.info(
                    'BTC_REGIME | %s → %s', self._regime.value, new_regime.value
                )
            self._regime = new_regime
            self._last_refresh = now
        except Exception as e:
            # Fail OPEN: keep the last known regime (or UNKNOWN) and try again
            # next interval. A market-data hiccup must never halt participation.
            logger.warning('BTC regime refresh failed (%s) — keeping %s',
                           e, self._regime.value)
            self._last_refresh = now
        return self._regime

    def _classify(self) -> BTCRegime:
        """Compute the BTC regime from 1h EMA200 (direction) + ADX (strength)."""
        df = self.exchange.fetch_ohlcv(
            self.settings.btc_regime_symbol, self.settings.trend_timeframe, limit=250
        )
        if df is None or len(df) < self.settings.ema_period:
            return BTCRegime.UNKNOWN

        ema = compute_ema(df['close'], period=self.settings.ema_period).dropna()
        adx = compute_adx(
            df['high'], df['low'], df['close'], period=self.settings.adx_period
        ).dropna()
        if ema.empty or adx.empty:
            return BTCRegime.UNKNOWN

        price = float(df['close'].iloc[-1])
        latest_ema = float(ema.iloc[-1])
        latest_adx = float(adx.iloc[-1])

        strong_trend = latest_adx > self.settings.adx_trend_threshold

        if strong_trend and price > latest_ema:
            return BTCRegime.UP_IMPULSE
        if strong_trend and price < latest_ema:
            return BTCRegime.DOWN_IMPULSE
        # Weak ADX or the ambiguous mid-zone → treat as SIDEWAYS (permissive).
        return BTCRegime.SIDEWAYS

    # ───────────────────────────────────────────
    # Gate
    # ───────────────────────────────────────────

    def allows(self, side: str) -> Tuple[bool, str]:
        """Decide whether a signal of the given side may proceed.

        Args:
            side: 'long' or 'short'.

        Returns:
            (allowed, reason). Permissive (True) in SIDEWAYS / UNKNOWN and when
            the filter is disabled. Blocks only counter-trend entries during a
            confirmed strong BTC trend.
        """
        if not self.settings.btc_regime_enabled:
            return True, 'btc_regime disabled'

        regime = self._regime

        if regime == BTCRegime.UP_IMPULSE and side == 'short':
            return False, 'BTC UP_IMPULSE — counter-trend SHORT blocked'
        if regime == BTCRegime.DOWN_IMPULSE and side == 'long':
            return False, 'BTC DOWN_IMPULSE — counter-trend LONG blocked'

        # SIDEWAYS, UNKNOWN, or trend-aligned → allow both directions.
        return True, f'BTC {regime.value} — {side} allowed'

    @property
    def regime(self) -> BTCRegime:
        """The current cached BTC regime (without triggering a refresh)."""
        return self._regime
