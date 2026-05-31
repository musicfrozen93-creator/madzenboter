"""
Zentry Futures Core — Position Manager.

Orchestrates position lifecycle: opening, recovery layers,
take-profit, stop-loss, and closing. The central coordinator
between grid, risk, and exchange modules.
"""

import logging
import time
from typing import List, Optional

from config.settings import Settings, VolatilityLevel
from core.database import Database
from core.models import Basket, RecoveryLayer, Signal, TradeRecord
from exchange.client import ExchangeClient
from exchange.utils import round_quantity, validate_min_notional
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from risk.stop_loss import StopLossManager
from signals.signal_engine import SignalEngine

logger = logging.getLogger(__name__)
trade_logger = logging.getLogger('trades')


class PositionManager:
    """Manages the full lifecycle of trading baskets.

    Responsibilities:
      • Open new positions on entry signals
      • Add recovery layers when price moves against
      • Monitor basket/individual TP and SL
      • Close positions (full basket or individual layers)
      • Coordinate with risk manager for all pre-trade checks
    """

    def __init__(
        self,
        exchange_client: ExchangeClient,
        settings: Settings,
        database: Database,
        risk_manager: RiskManager,
        position_sizer: PositionSizer,
        recovery_system: RecoverySystem,
        tp_manager: TakeProfitManager,
        sl_manager: StopLossManager,
        signal_engine: SignalEngine,
    ) -> None:
        self.exchange = exchange_client
        self.settings = settings
        self.database = database
        self.risk_manager = risk_manager
        self.position_sizer = position_sizer
        self.recovery = recovery_system
        self.tp_manager = tp_manager
        self.sl_manager = sl_manager
        self.signal_engine = signal_engine

    # ───────────────────────────────────────────
    # Open Position
    # ───────────────────────────────────────────

    def open_position(self, signal: Signal, balance: float) -> Optional[Basket]:
        """Open a new position (Layer 1) based on a signal.

        Performs full pre-trade validation via risk manager,
        sets leverage and margin mode, then places the market order.

        Args:
            signal: Entry signal from signal engine.
            balance: Current account balance.

        Returns:
            New Basket if successful, None if blocked or failed.
        """
        try:
            vol = VolatilityLevel(signal.volatility)
        except ValueError:
            vol = VolatilityLevel.MEDIUM

        leverage = self.settings.get_leverage(vol)
        base_margin = self.position_sizer.calculate_base_margin(balance, vol)

        # Layer 1 margin
        margin = base_margin * self.settings.recovery_margin_multipliers[0]

        # Get market info for quantity calculation
        try:
            market_info = self.exchange.get_symbol_info(signal.symbol)
        except Exception as e:
            logger.error('Failed to get market info for %s: %s', signal.symbol, e)
            return None

        quantity = self.position_sizer.calculate_quantity(
            margin, signal.current_price, leverage, market_info
        )

        if quantity <= 0:
            logger.warning('Calculated quantity is 0 for %s', signal.symbol)
            return None

        # Validate minimum notional
        if not validate_min_notional(quantity, signal.current_price, market_info):
            logger.warning(
                'Below min notional for %s: qty=%.8f × price=%.4f = %.4f',
                signal.symbol, quantity, signal.current_price,
                quantity * signal.current_price,
            )
            return None

        # Calculate current exposure from active baskets
        active_baskets = self.database.load_active_baskets()
        current_exposure = sum(b.total_margin for b in active_baskets)

        # Pre-trade risk check
        allowed, reason = self.risk_manager.can_open_position(
            margin, balance, current_exposure, len(active_baskets)
        )
        if not allowed:
            logger.info('Position blocked for %s: %s', signal.symbol, reason)
            return None

        # ── Execute ──
        try:
            self.exchange.set_margin_mode(signal.symbol, 'cross')
            self.exchange.set_leverage(signal.symbol, leverage)

            order_side = 'buy' if signal.side == 'long' else 'sell'
            order = self.exchange.place_market_order(
                signal.symbol, order_side, quantity
            )

            fill_price = float(
                order.get('average', order.get('price', signal.current_price)) or
                signal.current_price
            )

            layer = RecoveryLayer(
                layer_number=1,
                entry_price=fill_price,
                margin=margin,
                quantity=quantity,
                side=signal.side,
            )

            basket = Basket(
                symbol=signal.symbol,
                side=signal.side,
                atr_at_entry=signal.atr,
                volatility=signal.volatility,
                leverage=leverage,
            )
            basket.add_layer(layer)
            self.database.save_basket(basket)

            trade_logger.info(
                'OPEN %s %s L1 | price=%.4f qty=%.8f margin=%.4f '
                'lev=%dx vol=%s regime=%s | basket=%s',
                signal.side.upper(), signal.symbol, fill_price,
                quantity, margin, leverage, signal.volatility,
                signal.market_regime, basket.id[:8],
            )

            return basket

        except Exception as e:
            logger.error('Failed to open position for %s: %s', signal.symbol, e)
            return None

    # ───────────────────────────────────────────
    # Manage Baskets
    # ───────────────────────────────────────────

    def manage_baskets(
        self, baskets: List[Basket], balance: float
    ) -> List[Basket]:
        """Main management loop for all active baskets.

        Checks stop-losses (safety first), then take-profits,
        then recovery layer triggers.

        Args:
            baskets: List of active baskets.
            balance: Current account balance.

        Returns:
            Updated list of still-active baskets.
        """
        remaining: List[Basket] = []

        for basket in baskets:
            if basket.status != 'active' or basket.layer_count == 0:
                continue

            try:
                ticker = self.exchange.fetch_ticker(basket.symbol)
                current_price = ticker['last']

                if current_price <= 0:
                    remaining.append(basket)
                    continue

                atr = basket.atr_at_entry
                closed = False

                # ── PRIORITY 1: Stop Losses ──
                if self.sl_manager.check_emergency_sl(basket, current_price, balance):
                    self.close_basket(basket, 'emergency_sl')
                    closed = True
                elif self.sl_manager.check_basket_sl(basket, current_price):
                    self.close_basket(basket, 'basket_sl')
                    closed = True
                else:
                    # Individual SLs
                    for layer in list(basket.active_layers):
                        if self.sl_manager.check_individual_sl(
                            layer, current_price, atr, basket.side
                        ):
                            self._close_single_layer(basket, layer, current_price)
                    if basket.layer_count == 0:
                        basket.status = 'closed'
                        self.database.close_basket(basket.id)
                        closed = True

                if closed:
                    continue

                # ── PRIORITY 2: Take Profits ──
                if self.tp_manager.check_basket_tp(basket, current_price):
                    self.close_basket(basket, 'basket_tp')
                    continue

                # Partial close check
                partial_layers = self.tp_manager.check_partial_close(
                    basket, current_price
                )
                if partial_layers and len(partial_layers) < basket.layer_count:
                    # Close only the most profitable layer
                    self._close_single_layer(
                        basket, partial_layers[0], current_price
                    )

                # Individual TPs
                for layer in list(basket.active_layers):
                    if self.tp_manager.check_individual_tp(
                        layer, current_price, atr, basket.side
                    ):
                        self._close_single_layer(basket, layer, current_price)

                if basket.layer_count == 0:
                    basket.status = 'closed'
                    self.database.close_basket(basket.id)
                    continue

                # ── PRIORITY 3: Recovery Layers ──
                next_layer = self.recovery.check_recovery_trigger(
                    basket, current_price, atr
                )
                if next_layer is not None:
                    self._add_recovery_layer(basket, next_layer, balance, current_price)

                self.database.update_basket(basket)
                remaining.append(basket)

            except Exception as e:
                logger.error(
                    'Error managing basket %s (%s): %s',
                    basket.id[:8], basket.symbol, e,
                )
                remaining.append(basket)

        return remaining

    # ───────────────────────────────────────────
    # Close Operations
    # ───────────────────────────────────────────

    def close_basket(self, basket: Basket, reason: str) -> Optional[TradeRecord]:
        """Close an entire basket — all active layers.

        Args:
            basket: The basket to close.
            reason: Reason for closure (for trade record).

        Returns:
            TradeRecord if successful, None on error.
        """
        try:
            total_qty = basket.total_quantity
            if total_qty <= 0:
                basket.close_all()
                self.database.close_basket(basket.id)
                return None

            ticker = self.exchange.fetch_ticker(basket.symbol)
            current_price = ticker['last']

            # Close position on exchange
            for attempt in range(3):
                try:
                    self.exchange.close_position(
                        basket.symbol, basket.side, total_qty
                    )
                    break
                except Exception as e:
                    if attempt == 2:
                        logger.critical(
                            'FAILED to close basket %s after 3 attempts: %s',
                            basket.id[:8], e,
                        )
                        return None
                    logger.warning(
                        'Close attempt %d failed for %s: %s — retrying',
                        attempt + 1, basket.symbol, e,
                    )
                    time.sleep(1)

            # Calculate PnL
            unrealized = basket.unrealized_pnl(current_price)
            fee = total_qty * current_price * self.settings.taker_fee_pct * 2
            realized_pnl = unrealized - fee

            trade = TradeRecord(
                basket_id=basket.id,
                symbol=basket.symbol,
                side=basket.side,
                entry_price=basket.avg_entry_price,
                exit_price=current_price,
                quantity=total_qty,
                margin=basket.total_margin,
                leverage=basket.leverage,
                pnl=realized_pnl,
                fee=fee,
                layers_used=basket.layer_count,
                entry_time=basket.created_at,
                exit_time=time.time(),
                exit_reason=reason,
            )

            basket.close_all()
            self.database.close_basket(basket.id)
            self.database.save_trade(trade)

            pnl_symbol = '+' if realized_pnl >= 0 else ''
            trade_logger.info(
                'CLOSE %s %s [%s] | entry=%.4f exit=%.4f | '
                'PnL=%s%.4f USDT | layers=%d margin=%.4f fee=%.4f | basket=%s',
                basket.side.upper(), basket.symbol, reason.upper(),
                trade.entry_price, current_price,
                pnl_symbol, realized_pnl, basket.leverage,
                trade.margin, fee, basket.id[:8],
            )

            return trade

        except Exception as e:
            logger.error('Error closing basket %s: %s', basket.id[:8], e)
            return None

    def close_all_baskets(
        self, baskets: List[Basket], reason: str
    ) -> List[TradeRecord]:
        """Emergency close all active baskets.

        Args:
            baskets: List of all baskets.
            reason: Reason for mass closure.

        Returns:
            List of TradeRecord for successful closures.
        """
        trades: List[TradeRecord] = []
        for basket in baskets:
            if basket.status == 'active':
                trade = self.close_basket(basket, reason)
                if trade:
                    trades.append(trade)
        return trades

    # ───────────────────────────────────────────
    # Internal Helpers
    # ───────────────────────────────────────────

    def _close_single_layer(
        self, basket: Basket, layer: RecoveryLayer, current_price: float
    ) -> None:
        """Close a single layer within a basket.

        Args:
            basket: Parent basket.
            layer: The layer to close.
            current_price: Current market price.
        """
        try:
            self.exchange.close_position(
                basket.symbol, basket.side, layer.quantity
            )
            layer.status = 'closed'
            trade_logger.info(
                'CLOSE LAYER L%d %s %s | entry=%.4f exit=%.4f | basket=%s',
                layer.layer_number, basket.side.upper(), basket.symbol,
                layer.entry_price, current_price, basket.id[:8],
            )
        except Exception as e:
            logger.error(
                'Failed to close L%d for %s: %s',
                layer.layer_number, basket.symbol, e,
            )

    def _add_recovery_layer(
        self, basket: Basket, layer_number: int,
        balance: float, current_price: float,
    ) -> None:
        """Add a recovery layer to an existing basket.

        Args:
            basket: The basket to add a layer to.
            layer_number: The layer number to add (2, 3, or 4).
            balance: Current account balance.
            current_price: Current market price.
        """
        try:
            vol = VolatilityLevel(basket.volatility)
        except ValueError:
            vol = VolatilityLevel.MEDIUM

        base_margin = self.position_sizer.calculate_base_margin(balance, vol)
        layer_params = self.recovery.calculate_layer_params(
            basket, layer_number, base_margin, current_price, basket.leverage
        )

        # Validate with market info
        market_info = self.exchange.get_symbol_info(basket.symbol)
        layer_params.quantity = round_quantity(layer_params.quantity, market_info)

        if layer_params.quantity <= 0:
            logger.warning('Recovery L%d qty rounded to 0 for %s', layer_number, basket.symbol)
            return

        if not validate_min_notional(
            layer_params.quantity, current_price, market_info
        ):
            logger.warning('Recovery L%d below min notional for %s', layer_number, basket.symbol)
            return

        # Risk check for the additional margin
        active_baskets = self.database.load_active_baskets()
        current_exposure = sum(b.total_margin for b in active_baskets)
        allowed, reason = self.risk_manager.can_open_position(
            layer_params.margin, balance, current_exposure, len(active_baskets)
        )
        if not allowed:
            logger.info(
                'Recovery L%d blocked for %s: %s', layer_number, basket.symbol, reason
            )
            return

        # Execute
        try:
            order_side = 'buy' if basket.side == 'long' else 'sell'
            order = self.exchange.place_market_order(
                basket.symbol, order_side, layer_params.quantity
            )

            fill_price = float(
                order.get('average', order.get('price', current_price)) or current_price
            )
            layer_params.entry_price = fill_price

            basket.add_layer(layer_params)
            self.database.update_basket(basket)

            trade_logger.info(
                'RECOVERY L%d %s %s | price=%.4f qty=%.8f margin=%.4f | basket=%s',
                layer_number, basket.side.upper(), basket.symbol,
                fill_price, layer_params.quantity, layer_params.margin,
                basket.id[:8],
            )

        except Exception as e:
            logger.error(
                'Failed to add recovery L%d for %s: %s',
                layer_number, basket.symbol, e,
            )
