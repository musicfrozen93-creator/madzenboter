"""
ZenGrid — Position Sizer (FIXED sizing).

Every account uses EXACTLY the same sizing model. Position sizes are FIXED and
NEVER scaled by account balance:

  • Layer 1 margin = settings.layer1_margin_usd          (fixed USDT)
  • Layer 2 margin = 2 × Layer 1                          (the recovery layer)
  • quantity       = (margin × leverage) / price          (floored to lot step)

Account balance does not affect margin size, position count, recovery size, or
layer count. The only per-account variable is the admin-set leverage (3×–8×,
default 5×, never above 10×), which is resolved in Settings.
"""

import logging

from config.settings import Settings
from exchange.utils import round_quantity, validate_min_notional

logger = logging.getLogger(__name__)


class PositionSizer:
    """Calculates fixed margin/quantity for new entries and recovery layers."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def layer_margin(self, layer_number: int) -> float:
        """Fixed margin (USDT) for a 1-based layer (no balance dependence)."""
        return self.settings.get_layer_margin(layer_number)

    def build_order(
        self,
        layer_number: int,
        price: float,
        leverage: int,
        market_info: dict,
    ) -> dict:
        """Build a fixed-size order plan for a layer.

        Args:
            layer_number: 1-based layer (1 = initial entry, 2 = recovery layer).
            price: Current market price.
            leverage: Resolved account leverage.
            market_info: CCXT market dict (precision/limits).

        Returns:
            Dict: margin, notional, quantity, suitable (bool), reason (str).
        """
        margin = self.layer_margin(layer_number)
        result = {
            'margin': round(margin, 4),
            'notional': 0.0,
            'quantity': 0.0,
            'suitable': False,
            'reason': 'unknown',
        }

        if price <= 0 or leverage <= 0:
            result['reason'] = 'invalid price/leverage'
            return result

        notional = margin * leverage
        quantity = round_quantity(notional / price, market_info)

        result['notional'] = round(notional, 4)
        result['quantity'] = quantity

        if quantity <= 0:
            result['reason'] = 'quantity rounds to zero at fixed margin'
            return result

        if not validate_min_notional(quantity, price, market_info):
            min_notional = (
                market_info.get('limits', {}).get('cost', {}).get('min')
                or self.settings.min_notional_floor
            )
            result['reason'] = (
                f'below min notional ({quantity * price:.2f} < {min_notional:.2f}) '
                f'at fixed margin {margin:.2f} × {leverage}x'
            )
            return result

        result['suitable'] = True
        result['reason'] = 'OK'
        return result
