"""
ZenGrid — Position Sizer (FIXED, tier-driven sizing).

Sizing is FIXED per account tier and NEVER scaled by balance. The margin for a
layer comes from the basket's locked tier (Tier 1: L1 $2 / L2 $4; Tier 2: L1 $4
/ L2 $8) and is passed in by the position manager — this module only converts a
given margin into a valid exchange quantity:

  • quantity = (margin × leverage) / price   (floored to the lot step)

No percentage sizing, no balance scaling, no dynamic/adaptive/volatility sizing,
no martingale. The only per-account variable is the admin-set leverage (3×–8×,
default 5×, never above 10×), resolved in Settings.
"""

import logging

from config.settings import Settings
from exchange.utils import round_quantity, validate_min_notional

logger = logging.getLogger(__name__)


class PositionSizer:
    """Converts a FIXED tier margin into a valid exchange order quantity."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_order(
        self,
        margin: float,
        price: float,
        leverage: int,
        market_info: dict,
    ) -> dict:
        """Build a fixed-size order plan from an explicit (tier) margin.

        Args:
            margin: FIXED margin in USDT (from the basket's tier).
            price: Current market price.
            leverage: Resolved account leverage.
            market_info: CCXT market dict (precision/limits).

        Returns:
            Dict: margin, notional, quantity, suitable (bool), reason (str).
        """
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
        # Step size + precision: round_quantity floors the raw amount to the
        # exchange's lot step, guaranteeing a valid, submittable quantity.
        quantity = round_quantity(notional / price, market_info)

        result['notional'] = round(notional, 4)
        result['quantity'] = quantity

        limits = market_info.get('limits', {})
        min_qty = limits.get('amount', {}).get('min') or 0.0
        min_notional = limits.get('cost', {}).get('min') or self.settings.min_notional_floor

        # ── Exchange-safety validation (reject + log exact reason if invalid) ──
        if quantity <= 0:
            result['reason'] = (
                f'quantity rounds to zero at the lot step '
                f'(margin {margin:.2f} × {leverage}x / price {price:.6f})'
            )
            return result
        if min_qty and quantity < min_qty:
            result['reason'] = (
                f'below min quantity ({quantity:.8f} < {min_qty:.8f}) '
                f'at fixed margin {margin:.2f} × {leverage}x'
            )
            return result
        if not validate_min_notional(quantity, price, market_info):
            result['reason'] = (
                f'below min notional ({quantity * price:.2f} < {min_notional:.2f}) '
                f'at fixed margin {margin:.2f} × {leverage}x'
            )
            return result

        result['suitable'] = True
        result['reason'] = 'OK'
        return result
