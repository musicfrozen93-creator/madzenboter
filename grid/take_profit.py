"""
ZenGrid — Single-Entry Take-Profit / Stop-Loss Manager.

Each position is a single entry (no recovery, no layers, no averaging). It closes
on the FIRST of two conditions, both evaluated on NET profit
(unrealised PnL − estimated round-trip taker fees):

  TP) net PnL ≥ tp_margin_pct × margin   → reason 'tp'
  SL) net PnL ≤ −sl_margin_pct × margin  → reason 'sl'

With the approved spec (TP 25% / SL 12% of margin) a Tier-1 position
(margin $0.8) targets +$0.20 / −$0.096, and a Tier-2 position (margin $1.5)
targets +$0.375 / −$0.18. The take-profit sits at the account-level guards'
side: the daily loss limit and death-protection floor still fire first when
breached — the per-position SL only adds an earlier, bounded per-position cut.
"""

import logging
from typing import Optional, Tuple

from config.settings import Settings
from core.dto import Basket

logger = logging.getLogger(__name__)


class TakeProfitManager:
    """Fixed-% take-profit and stop-loss for a single-entry position."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def estimate_fees(self, basket: Basket, current_price: float) -> float:
        """Estimate round-trip taker fees for the position (USDT)."""
        qty = basket.total_quantity
        if qty <= 0 or current_price <= 0:
            return 0.0
        return qty * current_price * self.settings.taker_fee_pct * 2

    def net_pnl(self, basket: Basket, current_price: float) -> float:
        """Net position PnL (gross unrealised − estimated round-trip fees)."""
        return basket.unrealized_pnl(current_price) - self.estimate_fees(basket, current_price)

    def tp_target_usd(self, basket: Basket) -> float:
        """Net USDT profit target that closes this position (tp_margin_pct × margin)."""
        return self.settings.tp_margin_pct * basket.total_margin

    def sl_target_usd(self, basket: Basket) -> float:
        """Net USDT loss floor that stops this position (sl_margin_pct × margin)."""
        return self.settings.sl_margin_pct * basket.total_margin

    def check_take_profit(self, basket: Basket, current_price: float) -> bool:
        """True if the position's net profit has reached its TP target."""
        if basket.total_quantity <= 0:
            return False
        net = self.net_pnl(basket, current_price)
        target = self.tp_target_usd(basket)
        if target > 0 and net >= target:
            logger.info(
                'TAKE_PROFIT_HIT | symbol=%s direction=%s net=%.4f USDT (target=%.4f)',
                basket.symbol, basket.side.upper(), net, target,
            )
            return True
        return False

    def check_stop_loss(self, basket: Basket, current_price: float) -> bool:
        """True if the position's net loss has reached its SL floor."""
        if basket.total_quantity <= 0:
            return False
        target = self.sl_target_usd(basket)
        return target > 0 and self.net_pnl(basket, current_price) <= -target

    def evaluate_exit(
        self, basket: Basket, current_price: float
    ) -> Tuple[Optional[str], dict]:
        """Decide whether to close the position now, and why.

        Returns (exit_reason, metrics) where exit_reason is:
          • 'tp'   — net profit reached tp_margin_pct × margin
          • 'sl'   — net loss reached sl_margin_pct × margin
          • None   — no exit condition met
        """
        total_margin = basket.total_margin
        metrics = {
            'gross_pnl': 0.0, 'fee': 0.0, 'net_pnl': 0.0,
            'total_margin': total_margin, 'roi': 0.0,
            'tp_target': 0.0, 'sl_target': 0.0,
            'decision': 'hold',
        }
        if total_margin <= 0 or basket.total_quantity <= 0:
            return None, metrics

        gross = basket.unrealized_pnl(current_price)
        fee = self.estimate_fees(basket, current_price)
        net = gross - fee
        roi = net / total_margin
        tp_target = self.tp_target_usd(basket)
        sl_target = self.sl_target_usd(basket)
        metrics.update({
            'gross_pnl': gross, 'fee': fee, 'net_pnl': net, 'roi': roi,
            'tp_target': tp_target, 'sl_target': sl_target,
        })

        # TP and SL are mutually exclusive (TP target > 0 > −SL target).
        if tp_target > 0 and net >= tp_target:
            metrics['decision'] = 'tp'
            return 'tp', metrics
        if sl_target > 0 and net <= -sl_target:
            metrics['decision'] = 'sl'
            return 'sl', metrics

        return None, metrics
