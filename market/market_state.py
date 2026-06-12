"""
ZenGrid V2 — Market State Engine.

Computes the GLOBAL market context shared by all accounts:

  • BTC factor state — five-state machine (UP_IMPULSE / UP_DRIFT / RANGE /
    DOWN_DRIFT / DOWN_IMPULSE) derived from BTC's higher-timeframe structure.
    Crypto alts are dominated by a single shared factor (BTC); V1 had no
    awareness of it, which made counter-BTC alt baskets the dominant loss
    source. State transitions require closed-candle confirmation across
    consecutive updates so the state has persistence by construction.

  • Market breadth — percentage of watchlist symbols in UP trend states.
    Breadth turns BEFORE individual lagging EMA200s do, providing the early
    warning for factor regime turns.

  • Volatility regime — fast/slow blended estimate (COMPRESSION / NORMAL /
    EXPANSION) replacing the single short-window classifier.

Computed once per refresh interval and fanned out to all accounts.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

import pandas as pd

from config.settings import Settings
from exchange.client import ExchangeClient
from signals.indicators import compute_adx, compute_atr, compute_ema

logger = logging.getLogger(__name__)


class BtcFactorState(str, Enum):
    """Five-state BTC market-factor classification."""
    UP_IMPULSE = 'up_impulse'
    UP_DRIFT = 'up_drift'
    RANGE = 'range'
    DOWN_DRIFT = 'down_drift'
    DOWN_IMPULSE = 'down_impulse'
    UNKNOWN = 'unknown'


class VolRegime(str, Enum):
    """Blended fast/slow volatility regime."""
    COMPRESSION = 'compression'
    NORMAL = 'normal'
    EXPANSION = 'expansion'
    UNKNOWN = 'unknown'


@dataclass
class MarketState:
    """Snapshot of global market context, shared across all accounts."""

    btc_state: str = BtcFactorState.UNKNOWN.value
    vol_regime: str = VolRegime.UNKNOWN.value
    breadth_pct: float = 0.5        # fraction of watchlist in UP states
    breadth_direction: str = 'flat'  # 'rising' | 'falling' | 'flat'
    btc_adx: float = 0.0
    updated_at: float = field(default_factory=time.time)

    # ── Convenience helpers ──

    def is_up(self) -> bool:
        return self.btc_state in (
            BtcFactorState.UP_IMPULSE.value, BtcFactorState.UP_DRIFT.value
        )

    def is_down(self) -> bool:
        return self.btc_state in (
            BtcFactorState.DOWN_IMPULSE.value, BtcFactorState.DOWN_DRIFT.value
        )

    def is_impulse(self) -> bool:
        return self.btc_state in (
            BtcFactorState.UP_IMPULSE.value, BtcFactorState.DOWN_IMPULSE.value
        )

    def factor_direction(self) -> Optional[str]:
        """'long' if factor is up, 'short' if down, None if range/unknown."""
        if self.is_up():
            return 'long'
        if self.is_down():
            return 'short'
        return None

    def impulse_against(self, side: str) -> bool:
        """True when BTC is in an IMPULSE state opposing the given side."""
        if self.btc_state == BtcFactorState.UP_IMPULSE.value and side == 'short':
            return True
        if self.btc_state == BtcFactorState.DOWN_IMPULSE.value and side == 'long':
            return True
        return False

    def counter_factor(self, side: str) -> bool:
        """True when the side opposes the current factor direction."""
        direction = self.factor_direction()
        return direction is not None and side != direction


class MarketStateEngine:
    """Maintains the global MarketState from BTC data and symbol states.

    Refreshes at most every ``market_state_refresh_seconds`` (the update()
    call is cheap between refreshes). Factor-state transitions require
    ``factor_state_confirmations`` consecutive identical classifications on
    closed candles so single-candle noise cannot flip the regime.
    """

    def __init__(self, exchange_client: ExchangeClient, settings: Settings) -> None:
        self.exchange = exchange_client
        self.settings = settings
        self._state = MarketState()
        self._last_refresh: float = 0.0
        self._pending_state: Optional[str] = None
        self._pending_count: int = 0
        self._prev_breadth: Optional[float] = None
        self._btc_df_1h: Optional[pd.DataFrame] = None

    # ───────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────

    def get_state(self) -> MarketState:
        """Return the latest computed MarketState (may be stale-but-recent)."""
        return self._state

    def get_btc_df_1h(self) -> Optional[pd.DataFrame]:
        """Latest cached BTC 1h OHLCV frame (for relative-strength calcs)."""
        return self._btc_df_1h

    def update(self, symbol_states: Optional[Dict[str, str]] = None) -> MarketState:
        """Refresh the market state if the refresh interval has elapsed.

        Args:
            symbol_states: Mapping of symbol -> TrendState value string from
                the SymbolStateEngine, used for breadth. Optional.

        Returns:
            The current MarketState (refreshed or cached).
        """
        now = time.time()
        if now - self._last_refresh < self.settings.market_state_refresh_seconds:
            # Breadth can still be refreshed cheaply from symbol states.
            if symbol_states:
                self._update_breadth(symbol_states)
            return self._state

        self._last_refresh = now
        try:
            self._refresh_btc_state()
        except Exception as e:
            logger.warning('Market state refresh failed: %s', e)

        if symbol_states:
            self._update_breadth(symbol_states)

        self._state.updated_at = now
        logger.info(
            'MARKET_STATE | btc=%s adx=%.1f vol=%s breadth=%.0f%% (%s)',
            self._state.btc_state, self._state.btc_adx, self._state.vol_regime,
            self._state.breadth_pct * 100, self._state.breadth_direction,
        )
        return self._state

    # ───────────────────────────────────────────
    # Internal
    # ───────────────────────────────────────────

    def _refresh_btc_state(self) -> None:
        """Recompute BTC factor state and volatility regime from candles."""
        symbol = self.settings.btc_symbol

        df_4h = self.exchange.fetch_ohlcv(
            symbol, self.settings.factor_timeframe, limit=300
        )
        df_1h = self.exchange.fetch_ohlcv(symbol, '1h', limit=150)
        if df_1h is not None and len(df_1h) > 0:
            self._btc_df_1h = df_1h

        if df_4h is None or len(df_4h) < self.settings.factor_ema_slow + 5:
            logger.debug('Insufficient BTC %s data for factor state',
                         self.settings.factor_timeframe)
            return

        # Closed candles only — drop the still-forming bar.
        closed = df_4h.iloc[:-1]

        ema_fast = compute_ema(closed['close'], period=self.settings.factor_ema_fast)
        ema_slow = compute_ema(closed['close'], period=self.settings.factor_ema_slow)
        adx = compute_adx(
            closed['high'], closed['low'], closed['close'],
            period=self.settings.adx_period,
        )

        fast_clean = ema_fast.dropna()
        slow_clean = ema_slow.dropna()
        adx_clean = adx.dropna()
        if fast_clean.empty or slow_clean.empty or len(fast_clean) < 7:
            return

        price = float(closed['close'].iloc[-1])
        fast_now = float(fast_clean.iloc[-1])
        slow_now = float(slow_clean.iloc[-1])
        fast_prev = float(fast_clean.iloc[-7])  # slope over ~6 closed 4h bars
        latest_adx = float(adx_clean.iloc[-1]) if not adx_clean.empty else 0.0
        slope = (fast_now - fast_prev) / fast_now if fast_now > 0 else 0.0
        impulse = latest_adx > self.settings.factor_adx_impulse_threshold

        if price > slow_now and fast_now > slow_now:
            raw = (BtcFactorState.UP_IMPULSE if impulse and slope > 0
                   else BtcFactorState.UP_DRIFT)
        elif price < slow_now and fast_now < slow_now:
            raw = (BtcFactorState.DOWN_IMPULSE if impulse and slope < 0
                   else BtcFactorState.DOWN_DRIFT)
        else:
            raw = BtcFactorState.RANGE

        self._state.btc_adx = latest_adx
        self._apply_confirmed_state(raw.value)
        self._refresh_vol_regime()

    def _apply_confirmed_state(self, raw_state: str) -> None:
        """Apply hysteresis: a new state must repeat on consecutive
        refreshes before it replaces the current one."""
        current = self._state.btc_state
        if raw_state == current:
            self._pending_state = None
            self._pending_count = 0
            return

        if current == BtcFactorState.UNKNOWN.value:
            # First classification — accept immediately.
            self._state.btc_state = raw_state
            return

        if raw_state == self._pending_state:
            self._pending_count += 1
        else:
            self._pending_state = raw_state
            self._pending_count = 1

        if self._pending_count >= self.settings.factor_state_confirmations:
            logger.info(
                'BTC factor state transition: %s -> %s (confirmed %dx)',
                current, raw_state, self._pending_count,
            )
            self._state.btc_state = raw_state
            self._pending_state = None
            self._pending_count = 0

    def _refresh_vol_regime(self) -> None:
        """Blend fast vs slow BTC 1h ATR into a volatility regime."""
        df = self._btc_df_1h
        if df is None or len(df) < 110:
            return
        closed = df.iloc[:-1]
        atr = compute_atr(
            closed['high'], closed['low'], closed['close'],
            period=self.settings.atr_period,
        ).dropna()
        if len(atr) < 100:
            return
        fast = float(atr.iloc[-1])
        slow = float(atr.iloc[-100:].mean())
        if slow <= 0:
            return
        ratio = fast / slow
        if ratio > self.settings.vol_expansion_ratio:
            self._state.vol_regime = VolRegime.EXPANSION.value
        elif ratio < self.settings.vol_compression_ratio:
            self._state.vol_regime = VolRegime.COMPRESSION.value
        else:
            self._state.vol_regime = VolRegime.NORMAL.value

    def _update_breadth(self, symbol_states: Dict[str, str]) -> None:
        """Recompute watchlist breadth from per-symbol trend states."""
        if not symbol_states:
            return
        up_states = ('up', 'strong_up')
        known = [s for s in symbol_states.values() if s != 'unknown']
        if not known:
            return
        up = sum(1 for s in known if s in up_states)
        breadth = up / len(known)

        if self._prev_breadth is not None:
            delta = breadth - self._prev_breadth
            if delta > 0.05:
                self._state.breadth_direction = 'rising'
            elif delta < -0.05:
                self._state.breadth_direction = 'falling'
            else:
                self._state.breadth_direction = 'flat'
        self._prev_breadth = breadth
        self._state.breadth_pct = breadth
