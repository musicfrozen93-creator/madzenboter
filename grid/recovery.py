"""
Zentry Futures Core — Recovery System.

Controlled 2-layer averaging system (NOT martingale): an initial entry plus a
single recovery layer. The recovery layer is added when price moves against the
position by an ATR-based distance. There is no third or fourth layer.
"""

import logging
from typing import Optional

from config.settings import Settings
from core.dto import Basket, RecoveryLayer

logger = logging.getLogger(__name__)


class RecoverySystem:
    """Controlled recovery layer management (max 2 layers per basket).

    Layer parameters (balance-tier absolute margin):
      Layer 1 (initial entry): tier['layer1']  at entry
      Layer 2 (one recovery):  tier['layer2']  at 0.75 × ATR from Layer 1

        Tier A ($10–$50):   L1 $1.50  L2 $1.00  (cap $2.50)
        Tier B ($50–$200):  L1 $2.50  L2 $1.00  (cap $3.50)
        Tier C (> $200):    L1 $3.50  L2 $1.00  (cap $4.50)

    Per-layer margins come from settings.get_layer_margin(layer, balance) and
    their sum equals the tier basket cap. The Layer-2 trigger distance is
    measured from the Layer-1 entry price. No doubling or martingale.
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
        balance: float,
    ) -> RecoveryLayer:
        """Calculate parameters for a new recovery layer.

        Args:
            basket: The basket to add the layer to.
            layer_number: Layer index (1-based).
            base_margin: Base margin from position sizer.
            current_price: Current market price (entry price for this layer).
            leverage: Current leverage setting.
            balance: Current account balance (selects the sizing tier).

        Returns:
            RecoveryLayer with calculated margin and quantity.
        """
        # Balance-tier absolute per-layer margin. The tier's two layers sum to
        # the tier basket cap; the total basket margin is additionally
        # hard-capped against the tier cap in the position manager.
        margin = self.settings.get_layer_margin(layer_number, balance)
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
