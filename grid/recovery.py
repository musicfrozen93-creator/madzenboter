"""
Zentry Futures Core — Recovery System.

Controlled 4-layer averaging system (NOT martingale).
Layers are added when price moves against the position by
ATR-based distances with gentle margin progression.
"""

import logging
from typing import Optional

from config.settings import Settings
from core.dto import Basket, RecoveryLayer

logger = logging.getLogger(__name__)


class RecoverySystem:
    """Controlled recovery layer management.

    Layer parameters:
      Layer 1: base_margin × 1.00  at entry
      Layer 2: base_margin × 1.33  at 0.75 × ATR from Layer 1
      Layer 3: base_margin × 1.67  at 1.75 × ATR from Layer 1  (cumulative)
      Layer 4: base_margin × 2.17  at 3.00 × ATR from Layer 1  (cumulative)

    Distances are cumulative from Layer 1 entry price.
    Margin progression is gentle — no doubling or martingale.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.margin_multipliers = settings.recovery_margin_multipliers
        self.atr_distances = settings.recovery_atr_distances

    def check_recovery_trigger(
        self, basket: Basket, current_price: float, current_atr: float
    ) -> Optional[int]:
        """Check if price has moved enough to trigger the next recovery layer.

        Args:
            basket: The active basket to check.
            current_price: Current market price.
            current_atr: Current ATR value for distance calculation.

        Returns:
            Next layer number (2, 3, or 4) if triggered, None otherwise.
        """
        if basket.layer_count == 0:
            return None

        next_layer = basket.layer_count + 1
        if next_layer > self.settings.recovery_max_layers:
            return None

        if current_atr <= 0:
            return None

        # Layer 1 entry price is the anchor
        layer1 = basket.layers[0]
        entry_price = layer1.entry_price

        # Cumulative ATR distance from Layer 1
        cumulative_atr = sum(self.atr_distances[:next_layer])
        trigger_distance = cumulative_atr * current_atr

        if trigger_distance <= 0:
            return None

        if basket.side == 'long':
            trigger_price = entry_price - trigger_distance
            if current_price <= trigger_price:
                logger.info(
                    'Recovery trigger L%d for %s LONG: price=%.4f <= trigger=%.4f '
                    '(entry=%.4f - %.4f ATR)',
                    next_layer, basket.symbol, current_price, trigger_price,
                    entry_price, cumulative_atr,
                )
                return next_layer
        else:  # short
            trigger_price = entry_price + trigger_distance
            if current_price >= trigger_price:
                logger.info(
                    'Recovery trigger L%d for %s SHORT: price=%.4f >= trigger=%.4f '
                    '(entry=%.4f + %.4f ATR)',
                    next_layer, basket.symbol, current_price, trigger_price,
                    entry_price, cumulative_atr,
                )
                return next_layer

        return None

    def calculate_layer_params(
        self,
        basket: Basket,
        layer_number: int,
        base_margin: float,
        current_price: float,
        leverage: int,
    ) -> RecoveryLayer:
        """Calculate parameters for a new recovery layer.

        Args:
            basket: The basket to add the layer to.
            layer_number: Layer index (1-based).
            base_margin: Base margin from position sizer.
            current_price: Current market price (entry price for this layer).
            leverage: Current leverage setting.

        Returns:
            RecoveryLayer with calculated margin and quantity.
        """
        idx = layer_number - 1
        if idx >= len(self.margin_multipliers):
            idx = len(self.margin_multipliers) - 1

        margin = base_margin * self.margin_multipliers[idx]
        notional = margin * leverage
        quantity = notional / current_price if current_price > 0 else 0

        layer = RecoveryLayer(
            layer_number=layer_number,
            entry_price=current_price,
            margin=margin,
            quantity=quantity,
            side=basket.side,
        )

        logger.info(
            'Recovery L%d params: margin=%.4f qty=%.8f entry=%.4f leverage=%dx',
            layer_number, margin, quantity, current_price, leverage,
        )
        return layer
