"""
Zentry Futures Core — Take Profit Manager.

Manages basket TP (primary), individual TP (secondary),
and partial close logic for controlled profit taking.
"""

import logging
from typing import List

from config.settings import Settings, VolatilityLevel
from core.dto import Basket, RecoveryLayer

logger = logging.getLogger(__name__)


class TakeProfitManager:
    """Take-profit management for baskets and individual layers.

    Priority order:
      1. Basket TP — closes all layers when ROI target is reached
      2. Partial Close — closes profitable layers when 60%+ to target
      3. Individual TP — closes single layer at 2× ATR from entry
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check_basket_tp(self, basket: Basket, current_price: float) -> bool:
        """Check if the basket has reached its take-profit ROI target.

        The target ROI varies by volatility:
          LOW:    8%
          MEDIUM: 12%
          HIGH:   15%

        Args:
            basket: The active basket.
            current_price: Current market price.

        Returns:
            True if basket TP target is reached.
        """
        total_margin = basket.total_margin
        if total_margin <= 0:
            return False

        unrealized = basket.unrealized_pnl(current_price)
        roi = unrealized / total_margin

        try:
            vol = VolatilityLevel(basket.volatility)
        except ValueError:
            vol = VolatilityLevel.MEDIUM

        target_roi = self.settings.get_basket_tp_roi(vol)

        if roi >= target_roi:
            logger.info(
                'BASKET TP HIT: %s %s | ROI=%.2f%% (target=%.2f%%) | '
                'PnL=%.4f USDT | margin=%.4f',
                basket.side.upper(), basket.symbol,
                roi * 100, target_roi * 100,
                unrealized, total_margin,
            )
            return True
        return False

    def check_individual_tp(
        self, layer: RecoveryLayer, current_price: float, atr: float, side: str
    ) -> bool:
        """Check if an individual layer hit its TP (2× ATR from entry).

        Args:
            layer: The recovery layer to check.
            current_price: Current market price.
            atr: Current ATR value.
            side: Position side ('long' or 'short').

        Returns:
            True if individual TP is reached.
        """
        if atr <= 0:
            return False

        tp_distance = self.settings.individual_tp_atr_mult * atr

        if side == 'long':
            tp_price = layer.entry_price + tp_distance
            hit = current_price >= tp_price
        else:
            tp_price = layer.entry_price - tp_distance
            hit = current_price <= tp_price

        if hit:
            logger.info(
                'INDIVIDUAL TP L%d: %s entry=%.4f tp=%.4f price=%.4f',
                layer.layer_number, side.upper(),
                layer.entry_price, tp_price, current_price,
            )
        return hit

    def check_partial_close(
        self, basket: Basket, current_price: float
    ) -> List[RecoveryLayer]:
        """Check if profitable layers should be partially closed.

        Triggers when basket is 60%+ to its TP target and has
        multiple layers. Returns the profitable layers sorted
        by profit descending.

        Args:
            basket: The active basket.
            current_price: Current market price.

        Returns:
            List of profitable RecoveryLayer instances to close.
            Empty if partial close conditions are not met.
        """
        if basket.layer_count < 2:
            return []

        total_margin = basket.total_margin
        if total_margin <= 0:
            return []

        try:
            vol = VolatilityLevel(basket.volatility)
        except ValueError:
            vol = VolatilityLevel.MEDIUM

        target_roi = self.settings.get_basket_tp_roi(vol)
        unrealized = basket.unrealized_pnl(current_price)
        current_roi = unrealized / total_margin

        # Only consider partial close at 60%+ of target
        if current_roi < target_roi * 0.6:
            return []

        # Find profitable individual layers
        profitable: List[tuple[RecoveryLayer, float]] = []
        for layer in basket.active_layers:
            if basket.side == 'long':
                layer_pnl = (current_price - layer.entry_price) * layer.quantity
            else:
                layer_pnl = (layer.entry_price - current_price) * layer.quantity
            if layer_pnl > 0:
                profitable.append((layer, layer_pnl))

        # Sort by profit descending
        profitable.sort(key=lambda x: x[1], reverse=True)

        result = [layer for layer, _ in profitable]
        if result:
            logger.info(
                'Partial close candidate: %s %s | %d profitable layers | '
                'basket ROI=%.2f%% (60%% of target=%.2f%%)',
                basket.side.upper(), basket.symbol,
                len(result), current_roi * 100, target_roi * 100,
            )
        return result
