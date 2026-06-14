"""
Zentry Futures Core — Take Profit Manager.

Manages basket TP (primary), individual TP (secondary),
and partial close logic for controlled profit taking.
"""

import logging

from config.settings import Settings
from core.dto import Basket, RecoveryLayer

logger = logging.getLogger(__name__)


class TakeProfitManager:
    """Take-profit and profit-protection management for baskets.

    Priority order (highest first):
      1. Basket TP            — close all layers at the fixed 15% ROI target
      2. Profit Protection    — once armed at 10% ROI, close if ROI falls to 8%
      3. Individual TP        — close a single layer at 2× ATR from entry

    Profit protection replaces the old volatility-tiered TP and partial-close
    logic: it prevents winners turning into losers while still letting strong
    trends run all the way to the 15% target.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def basket_roi(basket: Basket, current_price: float) -> float:
        """Return the basket's current ROI (unrealised PnL / total margin)."""
        total_margin = basket.total_margin
        if total_margin <= 0:
            return 0.0
        return basket.unrealized_pnl(current_price) / total_margin

    def check_basket_tp(self, basket: Basket, current_price: float) -> bool:
        """Check if the basket has reached its fixed take-profit ROI target.

        The target is a flat 15% ROI for every basket (no volatility tiering).

        Args:
            basket: The active basket.
            current_price: Current market price.

        Returns:
            True if basket TP target is reached.
        """
        total_margin = basket.total_margin
        if total_margin <= 0:
            return False

        roi = self.basket_roi(basket, current_price)
        target_roi = self.settings.basket_tp_target_roi

        if roi >= target_roi:
            logger.info(
                'BASKET TP HIT: %s %s | ROI=%.2f%% (target=%.2f%%) | '
                'PnL=%.4f USDT | margin=%.4f',
                basket.side.upper(), basket.symbol,
                roi * 100, target_roi * 100,
                basket.unrealized_pnl(current_price), total_margin,
            )
            return True
        return False

    def evaluate_profit_protection(
        self, basket: Basket, current_price: float, already_armed: bool
    ) -> tuple[bool, bool]:
        """Evaluate the trailing profit-protection rule for a basket.

        Behaviour:
          • Arm when ROI first reaches `profit_protection_arm_roi` (10%). Once
            armed, the state stays armed (the caller persists it).
          • While armed, if ROI falls back to `profit_protection_floor_roi` (8%)
            or below, signal an immediate close to lock in profit.

        This is a pure function: it does not mutate the basket or touch the DB.
        The caller is responsible for persisting the returned armed flag.

        Args:
            basket: The active basket.
            current_price: Current market price.
            already_armed: Whether protection was previously armed (persisted).

        Returns:
            Tuple of (should_close, armed) where:
              should_close — True to close the basket now and lock profit.
              armed        — updated armed state to persist.
        """
        total_margin = basket.total_margin
        if total_margin <= 0:
            return False, already_armed

        roi = self.basket_roi(basket, current_price)
        arm_roi = self.settings.profit_protection_arm_roi
        floor_roi = self.settings.profit_protection_floor_roi

        armed = already_armed or roi >= arm_roi

        if not armed:
            return False, armed

        if not already_armed:
            logger.info(
                'PROFIT PROTECTION ARMED: %s %s | ROI=%.2f%% reached arm=%.2f%% '
                '(will lock if ROI falls to %.2f%%)',
                basket.side.upper(), basket.symbol,
                roi * 100, arm_roi * 100, floor_roi * 100,
            )

        if roi <= floor_roi:
            logger.info(
                'PROFIT PROTECTION TRIGGERED: %s %s | ROI=%.2f%% fell to floor '
                '%.2f%% — locking profit | PnL=%.4f USDT margin=%.4f',
                basket.side.upper(), basket.symbol,
                roi * 100, floor_roi * 100,
                basket.unrealized_pnl(current_price), total_margin,
            )
            return True, armed

        return False, armed

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
