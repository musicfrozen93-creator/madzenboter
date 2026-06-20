"""
ZenGrid — Recovery System (controlled, 2 layers max).

A controlled recovery basket — NOT a martingale and NOT a traditional grid:

  Layer 1 (initial entry): fixed margin
  Layer 2 (ONE recovery):  2 × Layer 1 margin, on a HYBRID trigger — whichever
                           occurs first of:
                             A) price moves ATR(14) × 2 against Layer 1, OR
                             B) Layer 1 floating loss ≥ recovery_loss_trigger_usd
                                (default $0.50).

Volatility-adjusted spacing (ATR-based), never fixed grid spacing. There is no
Layer 3, 4, or 5 — the maximum number of layers per basket is 2.
"""

import logging
from typing import Optional, Tuple

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

    def layer1_floating_pnl(self, basket: Basket, current_price: float) -> float:
        """Floating (unrealised) PnL of Layer 1 alone, in USDT (negative = loss)."""
        layer1 = basket.layers[0]
        if basket.side == 'long':
            return (current_price - layer1.entry_price) * layer1.quantity
        return (layer1.entry_price - current_price) * layer1.quantity

    def check_recovery_trigger(
        self, basket: Basket, current_price: float, atr: float
    ) -> Optional[Tuple[int, str]]:
        """Return (next_layer, trigger_type) if Layer 2 should activate, else None.

        HYBRID trigger — whichever occurs first:
          • 'ATR_TRIGGER'  — price moved ATR × layer2_atr_multiplier against L1, OR
          • 'LOSS_TRIGGER' — Layer 1 floating loss ≥ recovery_loss_trigger_usd.
        Never returns a layer beyond recovery_max_layers (2).
        """
        if basket.layer_count == 0:
            return None

        next_layer = basket.layer_count + 1
        if next_layer > self.settings.recovery_max_layers:
            return None  # NO Layer 3+ — ever.

        entry_price = basket.layers[0].entry_price
        distance = self.layer2_distance(atr)

        # Condition A: ATR-based adverse distance from the Layer-1 entry.
        atr_hit = False
        if distance > 0:
            if basket.side == 'long':
                atr_hit = current_price <= entry_price - distance
            else:
                atr_hit = current_price >= entry_price + distance

        # Condition B: Layer 1 floating loss ≥ the fixed USDT threshold.
        l1_pnl = self.layer1_floating_pnl(basket, current_price)
        loss_threshold = self.settings.recovery_loss_trigger_usd
        loss_hit = loss_threshold > 0 and l1_pnl <= -loss_threshold

        if not (atr_hit or loss_hit):
            return None

        trigger_type = 'ATR_TRIGGER' if atr_hit else 'LOSS_TRIGGER'
        if atr_hit and loss_hit:
            trigger_type = 'ATR_AND_LOSS'

        logger.info(
            'RECOVERY_TRIGGER | symbol=%s direction=%s layer=%d trigger=%s '
            'price=%.6f entry=%.6f atr_dist=%.6f l1_floating_pnl=%.4f loss_thr=%.2f',
            basket.symbol, basket.side.upper(), next_layer, trigger_type,
            current_price, entry_price, distance, l1_pnl, loss_threshold,
        )
        return next_layer, trigger_type

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
