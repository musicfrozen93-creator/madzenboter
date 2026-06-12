"""
ZenGrid V2 — Symbol State Engine.

Per-symbol trend state machine with hysteresis, replacing V1's binary
"price vs EMA200" check that flipped long/short eligibility on a single
tick crossing (the root cause of long→close→short flip losses).

States: STRONG_UP / UP / NEUTRAL / DOWN / STRONG_DOWN

  • A hysteresis band of EMA200(1h) ± hysteresis_atr_mult × ATR(1h)
    surrounds the EMA. Closes inside the band are NEUTRAL — an explicit,
    persistent state (routed to the RANGE template) rather than a
    knife-edge between long and short eligibility.
  • Directional states require a CLOSED 1h candle beyond the band.
  • STRONG_* additionally requires a meaningful EMA slope.

Also computes per-symbol relative strength vs BTC (return differential
over a lookback window) so the router can prefer relatively strong longs
and relatively weak shorts.

States are cached per symbol with a TTL so basket management can consult
them without re-fetching candles every loop.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional

import pandas as pd

from config.settings import Settings
from signals.indicators import compute_atr, compute_ema

logger = logging.getLogger(__name__)


class TrendState(str, Enum):
    """Hysteresis-based per-symbol trend classification."""
    STRONG_UP = 'strong_up'
    UP = 'up'
    NEUTRAL = 'neutral'
    DOWN = 'down'
    STRONG_DOWN = 'strong_down'
    UNKNOWN = 'unknown'


UP_STATES = (TrendState.UP.value, TrendState.STRONG_UP.value)
DOWN_STATES = (TrendState.DOWN.value, TrendState.STRONG_DOWN.value)


@dataclass
class SymbolSnapshot:
    """Cached classification for one symbol."""

    symbol: str
    state: str = TrendState.UNKNOWN.value
    relative_strength: float = 0.0  # return differential vs BTC (+ = stronger)
    ema200: float = 0.0
    atr_1h: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def supports(self, side: str) -> bool:
        """True if the state does NOT contradict the given side.

        NEUTRAL and UNKNOWN support both sides (range trading is allowed);
        only an opposing directional state invalidates a premise.
        """
        if side == 'long':
            return self.state not in DOWN_STATES
        return self.state not in UP_STATES

    def aligned(self, side: str) -> bool:
        """True if the state actively confirms the given side."""
        if side == 'long':
            return self.state in UP_STATES
        return self.state in DOWN_STATES


class SymbolStateEngine:
    """Classifies and caches per-symbol trend states with hysteresis."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._snapshots: Dict[str, SymbolSnapshot] = {}

    # ───────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────

    def classify(
        self,
        symbol: str,
        df_1h: pd.DataFrame,
        btc_df_1h: Optional[pd.DataFrame] = None,
    ) -> SymbolSnapshot:
        """Classify a symbol's trend state from 1h candles.

        Args:
            symbol: Trading pair.
            df_1h: 1-hour OHLCV DataFrame (will use closed candles only).
            btc_df_1h: BTC 1h OHLCV for relative-strength computation.

        Returns:
            SymbolSnapshot (also cached for later get() calls).
        """
        snapshot = SymbolSnapshot(symbol=symbol)

        if df_1h is None or len(df_1h) < self.settings.ema_period + 2:
            self._snapshots[symbol] = snapshot
            return snapshot

        closed = df_1h.iloc[:-1]  # closed candles only — no intra-candle flips

        ema = compute_ema(closed['close'], period=self.settings.ema_period).dropna()
        atr = compute_atr(
            closed['high'], closed['low'], closed['close'],
            period=self.settings.atr_period,
        ).dropna()

        if ema.empty or atr.empty:
            self._snapshots[symbol] = snapshot
            return snapshot

        price = float(closed['close'].iloc[-1])
        ema_now = float(ema.iloc[-1])
        atr_now = float(atr.iloc[-1])
        band = self.settings.hysteresis_atr_mult * atr_now

        # EMA slope over ~24 closed bars for the STRONG qualifier
        slope_pct = 0.0
        if len(ema) > 24 and ema_now > 0:
            slope_pct = (ema_now - float(ema.iloc[-25])) / ema_now

        if price > ema_now + band:
            if slope_pct > self.settings.strong_slope_pct:
                state = TrendState.STRONG_UP
            else:
                state = TrendState.UP
        elif price < ema_now - band:
            if slope_pct < -self.settings.strong_slope_pct:
                state = TrendState.STRONG_DOWN
            else:
                state = TrendState.DOWN
        else:
            state = TrendState.NEUTRAL

        snapshot.state = state.value
        snapshot.ema200 = ema_now
        snapshot.atr_1h = atr_now
        snapshot.relative_strength = self._relative_strength(closed, btc_df_1h)
        snapshot.updated_at = time.time()

        prev = self._snapshots.get(symbol)
        if prev and prev.state != snapshot.state and prev.state != 'unknown':
            logger.info(
                'SYMBOL_STATE %s: %s -> %s (price=%.4f ema=%.4f band=%.4f rs=%.4f)',
                symbol, prev.state, snapshot.state, price, ema_now, band,
                snapshot.relative_strength,
            )

        self._snapshots[symbol] = snapshot
        return snapshot

    def get(self, symbol: str) -> Optional[SymbolSnapshot]:
        """Return the cached snapshot for a symbol, or None."""
        return self._snapshots.get(symbol)

    def get_or_classify(
        self,
        symbol: str,
        fetch_1h: Callable[[str], pd.DataFrame],
        btc_df_1h: Optional[pd.DataFrame] = None,
    ) -> Optional[SymbolSnapshot]:
        """Return a fresh cached snapshot, re-classifying if stale.

        Used by basket management for symbols that may have rotated off the
        watchlist (no signal pass refreshes them anymore).

        Args:
            symbol: Trading pair.
            fetch_1h: Callable that fetches a 1h OHLCV DataFrame for a symbol.
            btc_df_1h: BTC 1h frame for relative strength.

        Returns:
            SymbolSnapshot or None if classification is impossible.
        """
        snap = self._snapshots.get(symbol)
        ttl = self.settings.symbol_state_ttl_seconds
        if snap and (time.time() - snap.updated_at) < ttl:
            return snap
        try:
            df = fetch_1h(symbol)
            return self.classify(symbol, df, btc_df_1h)
        except Exception as e:
            logger.debug('Symbol state refresh failed for %s: %s', symbol, e)
            return snap

    def snapshot_states(self) -> Dict[str, str]:
        """Mapping of symbol -> state value for breadth computation."""
        return {s.symbol: s.state for s in self._snapshots.values()}

    # ───────────────────────────────────────────
    # Internal
    # ───────────────────────────────────────────

    def _relative_strength(
        self, closed: pd.DataFrame, btc_df_1h: Optional[pd.DataFrame]
    ) -> float:
        """Return differential vs BTC over rs_lookback_bars closed 1h bars."""
        lookback = self.settings.rs_lookback_bars
        if btc_df_1h is None or len(btc_df_1h) < lookback + 2:
            return 0.0
        if len(closed) < lookback + 1:
            return 0.0
        try:
            btc_closed = btc_df_1h.iloc[:-1]
            sym_now = float(closed['close'].iloc[-1])
            sym_then = float(closed['close'].iloc[-(lookback + 1)])
            btc_now = float(btc_closed['close'].iloc[-1])
            btc_then = float(btc_closed['close'].iloc[-(lookback + 1)])
            if sym_then <= 0 or btc_then <= 0:
                return 0.0
            sym_ret = (sym_now - sym_then) / sym_then
            btc_ret = (btc_now - btc_then) / btc_then
            return round(sym_ret - btc_ret, 6)
        except Exception:
            return 0.0
