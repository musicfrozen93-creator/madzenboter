"""
ZenGrid V2 — Trade Templates & Router.

The central V2 mechanism: every signal V1 would take is still taken, but
ROUTED into one of three trade templates with different risk allocations.
The system never censors the trigger (trade frequency is preserved); it
decides how much size, how many recovery layers, and how much patience a
trade earns from market context.

  CORE  — signal aligned with both the symbol trend state and the BTC
          factor state. Full size, full recovery ladder, patient TP with
          trailing.
  SCOUT — signal counter to the BTC factor (e.g. an alt short during a
          BTC uptrend) or routed by post-loss direction demotion.
          Reduced size, one recovery layer, quick TP, hard invalidation
          exit. Taken — but never allowed to become a maximum-size loss.
  RANGE — symbol in the NEUTRAL hysteresis band and/or factor in RANGE.
          The V1 long→short flip zone becomes an explicit bidirectional
          range-trading mode with modest size and fast targets.

Rotation-tier watchlist symbols are capped below CORE so the expanded
watchlist adds frequency without adding full-size risk on lower-quality
symbols.
"""

import logging
from enum import Enum
from typing import Optional, Set, Tuple

from config.settings import Settings
from core.dto import Signal
from market.market_state import MarketState
from market.symbol_state import TrendState

logger = logging.getLogger(__name__)


class TradeTemplate(str, Enum):
    """Trade management template assigned at entry."""
    CORE = 'core'
    SCOUT = 'scout'
    RANGE = 'range'


class TemplateRouter:
    """Routes signals into trade templates from market/symbol alignment."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def route(
        self,
        signal: Signal,
        market_state: Optional[MarketState] = None,
        demoted_sides: Optional[Set[str]] = None,
    ) -> Tuple[TradeTemplate, float, str]:
        """Assign a template to a signal.

        Args:
            signal: The entry signal (carries symbol_state / btc_state /
                symbol_tier populated by the signal engine and engine loop).
            market_state: Current global MarketState (None = no V2 context;
                falls back to CORE, i.e. V1-equivalent behaviour).
            demoted_sides: Sides currently demoted by the risk manager's
                post-loss direction response.

        Returns:
            Tuple of (template, alignment_score, reason).
        """
        if market_state is None:
            return TradeTemplate.CORE, 0.5, 'no market state — legacy routing'

        side = signal.side
        symbol_state = signal.symbol_state or TrendState.UNKNOWN.value
        demoted_sides = demoted_sides or set()

        # ── 1. Post-loss direction demotion always wins ──
        if side in demoted_sides:
            return (
                TradeTemplate.SCOUT, 0.2,
                f'{side} demoted after consecutive losses',
            )

        counter_factor = market_state.counter_factor(side)
        factor_neutral = market_state.factor_direction() is None
        symbol_aligned = symbol_state in (
            ('up', 'strong_up') if side == 'long' else ('down', 'strong_down')
        )
        symbol_neutral = symbol_state in ('neutral', 'unknown')

        # ── 2. Counter-factor → SCOUT (the V1 alt-short trap, defused) ──
        if counter_factor:
            template = TradeTemplate.SCOUT
            score = 0.2
            reason = (
                f'counter-factor: {side} vs btc={market_state.btc_state}'
            )
        # ── 3. Neutral band / range factor → RANGE ──
        elif symbol_neutral or factor_neutral:
            template = TradeTemplate.RANGE
            score = 0.5
            reason = (
                f'range context: symbol={symbol_state} btc={market_state.btc_state}'
            )
        # ── 4. Fully aligned → CORE ──
        elif symbol_aligned:
            template = TradeTemplate.CORE
            score = self._core_score(signal, market_state)
            reason = (
                f'aligned: symbol={symbol_state} btc={market_state.btc_state}'
            )
        else:
            # Symbol state opposes the side (rare — EMA filter usually
            # prevents this); treat as scout, never full size.
            template = TradeTemplate.SCOUT
            score = 0.2
            reason = f'symbol state {symbol_state} opposes {side}'

        # ── 5. Watchlist tier cap: rotation-tier symbols never get CORE ──
        tier = getattr(signal, 'symbol_tier', 'core') or 'core'
        if tier == 'rotation' and template == TradeTemplate.CORE:
            template = TradeTemplate.RANGE
            reason += ' | rotation-tier capped below CORE'

        logger.info(
            'TEMPLATE_ROUTE %s %s -> %s (score=%.2f) | %s | rs=%.4f tier=%s',
            side.upper(), signal.symbol, template.value, score, reason,
            getattr(signal, 'relative_strength', 0.0), tier,
        )
        return template, score, reason

    def _core_score(self, signal: Signal, market_state: MarketState) -> float:
        """Alignment score for a CORE route (0.6–1.0).

        Strengthens with breadth confirmation and favourable relative
        strength (strong symbols for longs, weak symbols for shorts).
        """
        score = 0.7
        if signal.side == 'long' and market_state.breadth_pct > 0.6:
            score += 0.15
        elif signal.side == 'short' and market_state.breadth_pct < 0.4:
            score += 0.15
        rs = getattr(signal, 'relative_strength', 0.0) or 0.0
        if (signal.side == 'long' and rs > 0) or (signal.side == 'short' and rs < 0):
            score += 0.15
        return min(1.0, score)
