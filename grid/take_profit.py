"""
ZenGrid — Basket Take-Profit Manager.

The basket is the unit of profit-taking. The entire basket closes together on the
FIRST of two conditions (both use NET profit = unrealised PnL − round-trip fees):

  A) Fixed-USDT target (every basket), tier + layer-count specific:
       Tier 1  Layer 1 → $0.50   Layer 1 + Layer 2 → $1.50
       Tier 2  Layer 1 → $0.80   Layer 1 + Layer 2 → $2.00

  B) ROI target (EVERY basket — Layer-1-only and recovery):
       ROI = net basket PnL / total basket margin × 100
       Layer 1 only → layer1_roi_target  (Tier 1 12% → $0.24, Tier 2 10% → $0.40)
       Recovery     → recovery_roi_target (Tier 1 12% → $0.72, Tier 2 10% → $1.20)

The ROI target's dollar value is BELOW the matching USD target, so it lets a
profitable basket close earlier — freeing capital and improving turnover —
instead of waiting for the larger fixed-USD target. The exit reason is 'roi_l1'
for a Layer-1-only basket and 'roi_recovery' for a recovery basket.
"""

import logging
from typing import Optional, Tuple

from config.settings import Settings
from core.dto import Basket

logger = logging.getLogger(__name__)


class TakeProfitManager:
    """Tier-specific basket take-profit: fixed-USDT (all) + ROI (recovery)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def estimate_fees(self, basket: Basket, current_price: float) -> float:
        """Estimate round-trip taker fees for the whole basket (USDT)."""
        qty = basket.total_quantity
        if qty <= 0 or current_price <= 0:
            return 0.0
        return qty * current_price * self.settings.taker_fee_pct * 2

    def _basket_tier(self, basket: Basket) -> dict:
        """Return the basket's LOCKED tier (stored in basket.volatility)."""
        return (
            self.settings.get_tier_by_id(basket.volatility)
            or self.settings.account_tiers[0]
        )

    def target_usd(self, basket: Basket) -> float:
        """The net USDT profit target that closes this basket.

        Read from the basket's LOCKED tier and the live layer count, so once the
        recovery layer activates the target becomes the tier's recovery target.
        """
        tier = self._basket_tier(basket)
        if basket.layer_count >= 2:
            return float(tier['basket_tp_l2'])
        return float(tier['basket_tp_l1'])

    def net_pnl(self, basket: Basket, current_price: float) -> float:
        """Net basket PnL (gross unrealised − estimated round-trip fees)."""
        return basket.unrealized_pnl(current_price) - self.estimate_fees(basket, current_price)

    def basket_roi(self, basket: Basket, current_price: float) -> float:
        """Basket ROI as a fraction: net PnL / total basket margin."""
        total_margin = basket.total_margin
        if total_margin <= 0:
            return 0.0
        return self.net_pnl(basket, current_price) / total_margin

    def check_basket_tp(self, basket: Basket, current_price: float) -> bool:
        """True if the basket's net profit has reached its fixed-USDT target."""
        if basket.total_quantity <= 0:
            return False
        net = self.net_pnl(basket, current_price)
        target = self.target_usd(basket)
        if net >= target:
            logger.info(
                'BASKET_TP_HIT | symbol=%s direction=%s layers=%d net=%.4f USDT '
                '(target=%.2f)', basket.symbol, basket.side.upper(),
                basket.layer_count, net, target,
            )
            return True
        return False

    def evaluate_exit(
        self, basket: Basket, current_price: float
    ) -> Tuple[Optional[str], dict]:
        """Decide whether to close the basket now, and why.

        Returns (exit_reason, metrics) where exit_reason is:
          • 'roi_l1'       — Layer-1-only basket hit its tier Layer-1 ROI target
          • 'roi_recovery' — recovery basket (≥2 layers) hit its tier ROI target
          • 'basket_tp'    — basket hit its fixed-USDT target
          • None           — no exit condition met
        The ROI target is the lower (time-first) threshold, so it is evaluated
        first to honour "whichever occurs first".
        """
        total_margin = basket.total_margin
        metrics = {
            'gross_pnl': 0.0, 'fee': 0.0, 'net_pnl': 0.0,
            'total_margin': total_margin, 'roi': 0.0,
            'usd_target': 0.0, 'roi_target': 0.0, 'decision': 'hold',
        }
        if total_margin <= 0 or basket.total_quantity <= 0:
            return None, metrics

        gross = basket.unrealized_pnl(current_price)
        fee = self.estimate_fees(basket, current_price)
        net = gross - fee
        roi = net / total_margin
        usd_target = self.target_usd(basket)
        metrics.update({
            'gross_pnl': gross, 'fee': fee, 'net_pnl': net,
            'roi': roi, 'usd_target': usd_target,
        })

        tier = self._basket_tier(basket)
        # B) ROI target (every basket) — the lower, first-crossed threshold.
        if basket.layer_count >= 2:
            roi_target = float(tier.get('recovery_roi_target', 0.0))
            roi_reason = 'roi_recovery'
        else:
            roi_target = float(tier.get('layer1_roi_target', 0.0))
            roi_reason = 'roi_l1'
        metrics['roi_target'] = roi_target
        if roi_target > 0 and roi >= roi_target:
            metrics['decision'] = roi_reason
            return roi_reason, metrics

        # A) Fixed-USDT target (every basket).
        if net >= usd_target:
            metrics['decision'] = 'basket_tp'
            return 'basket_tp', metrics

        return None, metrics
