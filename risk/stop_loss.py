"""
Zentry Futures Core — Stop Loss Manager.

Implements three levels of stop-loss protection:
  1. Individual SL — per-layer stop at 3× ATR
  2. Basket SL — basket loss > 20% of total basket margin
  3. Emergency SL — single basket loss > 3% of account balance
"""

import logging

from config.settings import Settings
from core.dto import Basket, RecoveryLayer

logger = logging.getLogger(__name__)


class StopLossManager:
    """Three-tier stop-loss system for capital protection."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check_individual_sl(
        self, layer: RecoveryLayer, current_price: float, atr: float, side: str
    ) -> bool:
        """Check if an individual layer has hit its stop loss (3× ATR).

        Args:
            layer: The recovery layer to check.
            current_price: Current market price.
            atr: Current ATR value.
            side: Position side ('long' or 'short').

        Returns:
            True if stop loss is triggered.
        """
        if atr <= 0:
            return False

        sl_distance = self.settings.individual_sl_atr_mult * atr

        if side == 'long':
            sl_price = layer.entry_price - sl_distance
            hit = current_price <= sl_price
        else:
            sl_price = layer.entry_price + sl_distance
            hit = current_price >= sl_price

        if hit:
            logger.warning(
                'INDIVIDUAL SL L%d: %s entry=%.4f sl=%.4f price=%.4f',
                layer.layer_number, side.upper(),
                layer.entry_price, sl_price, current_price,
            )
        return hit

    def check_basket_sl(self, basket: Basket, current_price: float) -> bool:
        """Check if basket unrealised loss exceeds 20% of total margin.

        Args:
            basket: The active basket.
            current_price: Current market price.

        Returns:
            True if basket stop loss is triggered.
        """
        total_margin = basket.total_margin
        if total_margin <= 0:
            return False

        unrealized = basket.unrealized_pnl(current_price)
        if unrealized >= 0:
            return False

        loss_pct = abs(unrealized) / total_margin

        if loss_pct >= self.settings.basket_sl_pct:
            logger.warning(
                'BASKET SL: %s %s | loss=%.2f%% (limit=%.2f%%) | '
                'PnL=%.4f USDT | margin=%.4f',
                basket.side.upper(), basket.symbol,
                loss_pct * 100, self.settings.basket_sl_pct * 100,
                unrealized, total_margin,
            )
            return True
        return False

    def check_emergency_sl(
        self, basket: Basket, current_price: float, account_balance: float
    ) -> bool:
        """Check if basket loss exceeds 3% of total account balance.

        This is the last line of defence to protect the overall account.

        Args:
            basket: The active basket.
            current_price: Current market price.
            account_balance: Total account balance in USDT.

        Returns:
            True if emergency stop loss is triggered.
        """
        if account_balance <= 0:
            return True  # Emergency if balance is zero

        unrealized = basket.unrealized_pnl(current_price)
        if unrealized >= 0:
            return False

        loss_vs_account = abs(unrealized) / account_balance

        if loss_vs_account > self.settings.emergency_sl_account_pct:
            logger.critical(
                'EMERGENCY SL: %s %s | loss=%.4f USDT = %.2f%% of account '
                '(limit=%.2f%%) | balance=%.2f',
                basket.side.upper(), basket.symbol,
                abs(unrealized), loss_vs_account * 100,
                self.settings.emergency_sl_account_pct * 100,
                account_balance,
            )
            return True
        return False
