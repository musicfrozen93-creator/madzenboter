"""
ZenGrid V2 — Portfolio Manager.

Per-account portfolio-level risk controls. V1 treated every basket as an
independent risk; in practice 5 same-direction alt baskets are ~1.4
independent bets (one BTC-beta trade levered five times), which is why
losses arrived in correlated clusters.

The portfolio manager enforces three nested budgets ON TOP of the
existing per-basket and account-level limits:

  1. Total notional cap — beta exposure measured in notional (margin ×
     leverage), the quantity the market actually charges, not margin.
  2. Counter-factor notional cap — positions opposing the BTC factor
     direction share a deliberately smaller budget.
  3. Correlation cluster caps — max CORE-sized baskets per cluster per
     direction; overflow is demoted to SCOUT, not rejected.
  4. Event risk budget — rolling-window realized losses plus open basket
     risk budgets may not exceed a fixed fraction of the account; when
     consumed, counter-factor entries are blocked and aligned entries are
     demoted.

Design rule: ROUTE AND RESIZE, almost never reject — trade frequency is
preserved; what changes is how much risk a crowded direction can absorb.
"""

import logging
import time
from typing import List, Optional, Tuple

from config.settings import Settings
from core.dto import Basket, Signal
from grid.templates import TradeTemplate
from market.market_state import MarketState

logger = logging.getLogger(__name__)


class PortfolioManager:
    """Portfolio-level budget enforcement for a single account."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ───────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────

    def evaluate(
        self,
        signal: Signal,
        template: TradeTemplate,
        planned_margin: float,
        leverage: int,
        planned_risk: float,
        active_baskets: List[Basket],
        balance: float,
        market_state: Optional[MarketState] = None,
        recent_realized_losses: float = 0.0,
    ) -> Tuple[Optional[TradeTemplate], str]:
        """Evaluate a planned entry against portfolio budgets.

        Args:
            signal: The entry signal.
            template: Template assigned by the router.
            planned_margin: Planned first-layer margin (for this template).
            leverage: Planned leverage.
            planned_risk: Planned basket risk budget in USDT.
            active_baskets: This account's active baskets.
            balance: Current account balance.
            market_state: Global market state (None = checks that need the
                factor direction are skipped).
            recent_realized_losses: Sum of negative realized PnL within the
                event window (positive number).

        Returns:
            (final_template, reason) — final_template is None if blocked.
            The final template may be a demotion of the input template.
        """
        if balance <= 0:
            return None, 'invalid balance'

        planned_notional = planned_margin * leverage
        current_notional = sum(
            b.total_margin * max(b.leverage, 1) for b in active_baskets
        )

        # ── 1. Total notional cap ──
        max_notional = balance * self.settings.max_total_notional_mult
        if current_notional + planned_notional > max_notional:
            if template != TradeTemplate.SCOUT:
                logger.info(
                    'PORTFOLIO demote %s %s: notional %.2f+%.2f > cap %.2f -> SCOUT',
                    signal.side.upper(), signal.symbol,
                    current_notional, planned_notional, max_notional,
                )
                return TradeTemplate.SCOUT, 'total notional cap — demoted to scout'
            return None, (
                f'total notional cap reached '
                f'({current_notional:.2f}+{planned_notional:.2f} > {max_notional:.2f})'
            )

        # ── 2. Counter-factor notional cap ──
        # PARTICIPATION-REGRESSION FIX: wind-down baskets are excluded from
        # the counter-factor sum. After a factor flip, the formerly-aligned
        # book becomes counter-factor AND winds down simultaneously — those
        # baskets are terminating, cannot add layers, and counting their
        # notional here hard-blocked every new entry on that side until
        # they finished exiting. The cap exists to limit NEW counter-trend
        # exposure growth; terminating exposure stays counted in the total
        # notional cap, max positions, and the event risk budget.
        if market_state is not None and market_state.counter_factor(signal.side):
            counter_side = signal.side
            counter_notional = sum(
                b.total_margin * max(b.leverage, 1)
                for b in active_baskets
                if b.side == counter_side and not getattr(b, 'wind_down', False)
            )
            counter_cap = balance * self.settings.counter_factor_notional_cap_mult
            if counter_notional + planned_notional > counter_cap:
                return None, (
                    f'counter-factor notional cap reached for {counter_side} '
                    f'({counter_notional:.2f}+{planned_notional:.2f} > {counter_cap:.2f})'
                )

        # ── 3. Correlation cluster cap (CORE-size concentration) ──
        if template == TradeTemplate.CORE:
            cluster = self._cluster_of(signal.symbol)
            core_in_cluster = sum(
                1 for b in active_baskets
                if b.side == signal.side
                and (getattr(b, 'template', 'core') or 'core') == 'core'
                and self._cluster_of(b.symbol) == cluster
            )
            if core_in_cluster >= self.settings.max_core_per_cluster_direction:
                logger.info(
                    'PORTFOLIO demote %s %s: %d CORE baskets already %s in '
                    'cluster %r -> SCOUT',
                    signal.side.upper(), signal.symbol, core_in_cluster,
                    signal.side, cluster,
                )
                return (
                    TradeTemplate.SCOUT,
                    f'cluster {cluster!r} CORE cap — demoted to scout',
                )

        # ── 4. Event risk budget ──
        open_risk = sum(
            getattr(b, 'risk_budget', 0.0) or 0.0 for b in active_baskets
        )
        event_budget = balance * self.settings.event_risk_budget_pct
        committed = recent_realized_losses + open_risk + planned_risk
        if committed > event_budget:
            counter = (
                market_state is not None
                and market_state.counter_factor(signal.side)
            )
            if counter:
                return None, (
                    f'event risk budget consumed '
                    f'({committed:.2f} > {event_budget:.2f}) — counter-factor blocked'
                )
            if template != TradeTemplate.SCOUT:
                logger.info(
                    'PORTFOLIO demote %s %s: event budget %.2f > %.2f -> SCOUT',
                    signal.side.upper(), signal.symbol, committed, event_budget,
                )
                return TradeTemplate.SCOUT, 'event risk budget — demoted to scout'
            return None, (
                f'event risk budget consumed ({committed:.2f} > {event_budget:.2f})'
            )

        return template, 'OK'

    @staticmethod
    def recent_realized_losses(database, window_hours: float) -> float:
        """Sum of |negative PnL| from trades closed within the window.

        Args:
            database: Database (or account-scoped wrapper) exposing
                get_trades_since().
            window_hours: Lookback window in hours.

        Returns:
            Positive total of realized losses in USDT (0.0 on failure).
        """
        try:
            since = time.time() - window_hours * 3600.0
            trades = database.get_trades_since(since)
            return float(sum(-t.pnl for t in trades if t.pnl < 0))
        except Exception as e:
            logger.debug('recent_realized_losses failed: %s', e)
            return 0.0

    # ───────────────────────────────────────────
    # Internal
    # ───────────────────────────────────────────

    def _cluster_of(self, symbol: str) -> str:
        """Correlation cluster for a symbol (default cluster: 'alt')."""
        base = symbol.split('/')[0].upper()
        return self.settings.symbol_clusters.get(base, 'alt')
