"""
ZenGrid — Recovery System (controlled, 2 layers max).

A controlled recovery basket — NOT a martingale and NOT a traditional grid:

  Layer 1 (initial entry): fixed margin                     at the entry price
  Layer 2 (ONE recovery):  2 × Layer 1 margin               when Layer 1 drawdown
                                                            exceeds ATR(14) × 2

Volatility-adjusted spacing (ATR-based), never fixed grid spacing. There is no
Layer 3, 4, or 5 — the maximum number of layers per basket is 2.
"""

import logging
from typing import Optional

from config.settings import Settings
from core.dto import Basket, RecoveryLayer

logger = logging.getLogger(__name__)


class RecoverySystem:
    """Controlled recovery-layer management (max 2 layers per basket)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def layer2_distance(self, atr: float) -> float:
        """Adverse price distance that activates Layer 2: ATR(14) × multiplier."""
        return max(0.0, atr) * self.settings.layer2_atr_multiplier

    def check_recovery_trigger(
        self, basket: Basket, current_price: float, atr: float
    ) -> Optional[int]:
        """Return the next layer number (2) if Layer 2 should activate, else None.

        Layer 2 activates only when Layer 1's drawdown exceeds the ATR-based
        distance (ATR × layer2_atr_multiplier) measured from the Layer-1 entry.
        Never returns a layer beyond recovery_max_layers (2).
        """
        if basket.layer_count == 0:
            return None

        next_layer = basket.layer_count + 1
        if next_layer > self.settings.recovery_max_layers:
            return None  # NO Layer 3+ — ever.

        distance = self.layer2_distance(atr)
        if distance <= 0:
            return None

        entry_price = basket.layers[0].entry_price

        if basket.side == 'long':
            trigger_price = entry_price - distance
            if current_price <= trigger_price:
                logger.info(
                    'RECOVERY_TRIGGER | symbol=%s direction=LONG layer=%d '
                    'price=%.6f <= trigger=%.6f (entry=%.6f - %.2f×ATR)',
                    basket.symbol, next_layer, current_price, trigger_price,
                    entry_price, self.settings.layer2_atr_multiplier,
                )
                return next_layer
        else:  # short
            trigger_price = entry_price + distance
            if current_price >= trigger_price:
                logger.info(
                    'RECOVERY_TRIGGER | symbol=%s direction=SHORT layer=%d '
                    'price=%.6f >= trigger=%.6f (entry=%.6f + %.2f×ATR)',
                    basket.symbol, next_layer, current_price, trigger_price,
                    entry_price, self.settings.layer2_atr_multiplier,
                )
                return next_layer

        return None

    def build_layer(
        self,
        basket: Basket,
        layer_number: int,
        margin: float,
        quantity: float,
        entry_price: float,
    ) -> RecoveryLayer:
        """Construct a RecoveryLayer DTO for a recovery layer."""
        layer = RecoveryLayer(
            layer_number=layer_number,
            entry_price=entry_price,
            margin=margin,
            quantity=quantity,
            side=basket.side,
        )
        logger.info(
            'RECOVERY_LAYER_BUILT | symbol=%s layer=%d margin=%.4f qty=%.8f entry=%.6f',
            basket.symbol, layer_number, margin, quantity, entry_price,
        )
        return layer
