"""
ZenGrid — Basket Take-Profit Manager.

The basket is the unit of profit-taking. The entire basket is closed together
when its NET profit (unrealised PnL minus estimated round-trip fees) reaches the
TIER-SPECIFIC USDT target read from the basket's locked tier:

  Tier 1   Layer 1 only → $0.50    Layer 1 + Layer 2 → $1.50
  Tier 2   Layer 1 only → $0.80    Layer 1 + Layer 2 → $2.00

There is no per-layer take-profit, no ROI-percentage target, and no trailing
profit lock — the basket closes as a whole at the dollar target.
"""

import logging

from config.settings import Settings
from core.dto import Basket

logger = logging.getLogger(__name__)


class TakeProfitManager:
    """Fixed-dollar, tier-specific basket take-profit."""

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

    def check_basket_tp(self, basket: Basket, current_price: float) -> bool:
        """True if the basket's net profit has reached its USDT target."""
        if basket.total_quantity <= 0:
            return False

        gross = basket.unrealized_pnl(current_price)
        net = gross - self.estimate_fees(basket, current_price)
        target = self.target_usd(basket)

        if net >= target:
            logger.info(
                'BASKET_TP_HIT | symbol=%s direction=%s layers=%d net=%.4f USDT '
                '(target=%.2f gross=%.4f)',
                basket.symbol, basket.side.upper(), basket.layer_count,
                net, target, gross,
            )
            return True
        return False
