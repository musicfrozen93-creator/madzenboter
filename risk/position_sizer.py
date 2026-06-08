"""
Zentry Futures Core — Position Sizer.

Dynamic position sizing based on account balance tier and volatility.
Prioritises capital preservation over aggressive sizing.
"""

import logging

from config.settings import Settings, VolatilityLevel
from exchange.utils import round_quantity

logger = logging.getLogger(__name__)


class PositionSizer:
    """Calculates position size (margin and quantity) for new entries.

    Tier-based sizing:
      < 50 USDT:  0.40–0.60 margin, max 3 positions
      50–100:     0.60–0.80 margin, max 5 positions
      > 100:      0.90–1.50 margin, max 8 positions

    High volatility uses the lower end of the range (conservative).
    Low volatility uses the upper end (slightly more aggressive).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def calculate_base_margin(
        self, balance: float, volatility: VolatilityLevel
    ) -> float:
        """Calculate base margin for a new position.

        Scales linearly within the tier's margin range, adjusted
        by volatility to be more conservative in volatile markets.

        Args:
            balance: Current account balance in USDT.
            volatility: Current volatility classification.

        Returns:
            Base margin amount in USDT.
        """
        min_margin, max_margin = self.settings.get_base_margin_range(balance)
        tier = self.settings.get_tier(balance)

        # Position within the tier (0.0 = bottom, 1.0 = top)
        tier_list = self.settings.position_margin_tiers
        tier_idx = tier_list.index(tier) if tier in tier_list else 0

        if tier_idx == 0:
            # First tier: scale from 30 to max_balance
            tier_bottom = 0.0
        else:
            tier_bottom = tier_list[tier_idx - 1]['max_balance']

        tier_top = tier['max_balance']
        if tier_top == float('inf'):
            # For the unbounded tier, scale up to 5× the bottom
            tier_top = max(tier_bottom * 5, 500)

        if tier_top > tier_bottom:
            tier_position = min(
                1.0, (balance - tier_bottom) / (tier_top - tier_bottom)
            )
        else:
            tier_position = 0.5

        # Base margin interpolated within range
        base = min_margin + (max_margin - min_margin) * tier_position

        # Volatility adjustment
        if volatility == VolatilityLevel.HIGH:
            base = min_margin + (base - min_margin) * 0.3  # conservative
        elif volatility == VolatilityLevel.LOW:
            base = base + (max_margin - base) * 0.3  # slightly more aggressive

        # Absolute minimum: 0.30 USDT (below this Binance will reject)
        base = max(0.30, base)

        # Never use more than 5% of balance on a single layer-1 entry
        max_single = balance * 0.05
        base = min(base, max_single) if max_single > 0.30 else base

        logger.debug(
            'Position size: balance=%.2f vol=%s → margin=%.4f (range=%.2f–%.2f)',
            balance, volatility.value, base, min_margin, max_margin,
        )
        return round(base, 4)

    def calculate_quantity(
        self, margin: float, price: float, leverage: int, market_info: dict
    ) -> float:
        """Calculate order quantity from margin, price, and leverage.

        Args:
            margin: Margin to allocate in USDT.
            price: Current market price.
            leverage: Leverage multiplier.
            market_info: CCXT market info dict for precision.

        Returns:
            Quantity rounded down to exchange precision.
        """
        if price <= 0 or leverage <= 0:
            return 0.0

        notional = margin * leverage
        quantity = notional / price
        quantity = round_quantity(quantity, market_info)
        return quantity

    def get_max_positions(self, balance: float) -> int:
        """Maximum simultaneous baskets allowed for the balance.

        Args:
            balance: Current account balance.

        Returns:
            Maximum position count.
        """
        return self.settings.get_max_positions(balance)
