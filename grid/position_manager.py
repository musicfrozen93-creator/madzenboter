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
from core.dto import Basket, RecoveryLayer, Signal, TradeRecord
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

        logger.info(
            'POSITION_OPEN_REQUEST %s %s | balance=%.2f vol=%s lev=%dx price=%.4f',
            signal.side.upper(), signal.symbol, balance, vol.value, leverage,
            signal.current_price,
        )

        # Get market info for quantity calculation
        try:
            market_info = self.exchange.get_symbol_info(signal.symbol)
        except Exception as e:
            logger.error('Failed to get market info for %s: %s', signal.symbol, e)
            logger.info('SIGNAL_REJECTED %s | stage=market_info | %s', signal.symbol, e)
            return None

        # ── Account-size-aware order plan + symbol suitability ──
        # evaluate_entry accounts for the exchange min-notional / min-lot, so the
        # margin/notional below reflect the SMALLEST order that would actually
        # fill — and reject the symbol if that breaches the account hard cap or
        # sits too close to liquidation.
        plan = self.position_sizer.evaluate_entry(
            balance, signal.current_price, leverage, vol, market_info
        )
        quantity = plan['quantity']
        margin = plan['margin']

        if not plan['suitable']:
            logger.info(
                'SIGNAL_REJECTED %s | stage=suitability | reason=%s | balance=%.2f '
                'price=%.4f lev=%dx req_margin=%.2f notional=%.2f liq_dist=%.1f%% hard_cap=%.2f',
                signal.symbol, plan['reason'], balance, signal.current_price,
                leverage, margin, plan['notional'],
                plan['liquidation_distance_pct'] * 100, plan['hard_cap'],
            )
            return None

        # Calculate current exposure from active baskets
        active_baskets = self.database.load_active_baskets()
        current_exposure = sum(b.total_margin for b in active_baskets)

        # Existing-basket protection: never open a second basket on the same symbol.
        if any(b.symbol == signal.symbol for b in active_baskets):
            logger.info(
                'SIGNAL_REJECTED %s | stage=existing_basket | an active basket already exists for this symbol',
                signal.symbol,
            )
            return None

        # Same-symbol cooldown: block re-entry for a window after the previous
        # basket on this symbol closed (per-account, persisted, any exit reason).
        remaining_cd = self._cooldown_remaining(signal.symbol)
        if remaining_cd > 0:
            logger.info(
                'SIGNAL_REJECTED %s | stage=cooldown | %.0fs remaining of %ds '
                'same-symbol cooldown after last basket close',
                signal.symbol, remaining_cd, self.settings.symbol_cooldown_seconds,
            )
            return None

        # Pre-trade risk check
        allowed, reason = self.risk_manager.can_open_position(
            margin, balance, current_exposure, len(active_baskets)
        )
        if not allowed:
            logger.info(
                'SIGNAL_REJECTED %s | stage=risk | reason=%s | margin=%.2f exposure=%.2f positions=%d',
                signal.symbol, reason, margin, current_exposure, len(active_baskets),
            )
            return None

        logger.info(
            'SIGNAL_ACCEPTED %s %s | balance=%.2f lev=%dx | margin=%.2f '
            'notional=%.2f qty=%.8f liq_dist=%.1f%% hard_cap=%.2f',
            signal.side.upper(), signal.symbol, balance, leverage, margin,
            plan['notional'], quantity, plan['liquidation_distance_pct'] * 100,
            plan['hard_cap'],
        )

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

            logger.info(
                'POSITION_OPEN_SUCCESS %s %s | basket=%s price=%.4f qty=%.8f margin=%.2f lev=%dx',
                signal.side.upper(), signal.symbol, basket.id[:8],
                fill_price, quantity, margin, leverage,
            )
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
            logger.info('SIGNAL_REJECTED %s | stage=order_submission | %s', signal.symbol, e)
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
                        self._finalize_closed_basket(basket)
                        closed = True

                if closed:
                    continue

                # ── PRIORITY 2: Take Profit + Profit Protection ──
                # 2a. Fixed 15% ROI basket take-profit (closes the whole basket).
                if self.tp_manager.check_basket_tp(basket, current_price):
                    self.close_basket(basket, 'basket_tp')
                    continue

                # 2b. Trailing profit protection: arm at 10% ROI; once armed, lock
                #     in profit by closing immediately if ROI falls back to 8%.
                #     The armed flag is persisted (per-account, survives restart).
                armed_key = self._armed_key(basket.id)
                already_armed = self.database.get_state(armed_key) == 'true'
                should_close, armed = self.tp_manager.evaluate_profit_protection(
                    basket, current_price, already_armed
                )
                if armed and not already_armed:
                    self.database.set_state(armed_key, 'true')
                if should_close:
                    self.close_basket(basket, 'profit_protection')
                    continue

                # 2c. Individual layer TPs (per-layer profit taking on recovery).
                for layer in list(basket.active_layers):
                    if self.tp_manager.check_individual_tp(
                        layer, current_price, atr, basket.side
                    ):
                        self._close_single_layer(basket, layer, current_price)

                if basket.layer_count == 0:
                    self._finalize_closed_basket(basket)
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
                self._finalize_closed_state(basket.symbol, basket.id)
                return None

            ticker = self.exchange.fetch_ticker(basket.symbol)
            current_price = ticker['last']

            # Close position on exchange. A reduce-only close that fails because
            # the position is already gone is BENIGN: the exit already happened,
            # so we still finalize the basket rather than leaving it stale.
            for attempt in range(3):
                try:
                    self.exchange.close_position(
                        basket.symbol, basket.side, total_qty
                    )
                    break
                except Exception as e:
                    if self._is_benign_close_error(e):
                        logger.warning(
                            'Close for %s reports position already flat (%s) — '
                            'treating basket %s as closed.',
                            basket.symbol, e, basket.id[:8],
                        )
                        break
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
            self._finalize_closed_state(basket.symbol, basket.id)

            pnl_symbol = '+' if realized_pnl >= 0 else ''
            trade_logger.info(
                'CLOSE %s %s [%s] | entry=%.4f exit=%.4f | '
                'PnL=%s%.4f USDT | lev=%dx layers=%d margin=%.4f fee=%.4f | basket=%s',
                basket.side.upper(), basket.symbol, reason.upper(),
                trade.entry_price, current_price,
                pnl_symbol, realized_pnl, basket.leverage, trade.layers_used,
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
    # Cooldown & Profit-Protection State (persisted, per-account)
    # ───────────────────────────────────────────

    @staticmethod
    def _cooldown_key(symbol: str) -> str:
        """State key for a symbol's re-entry cooldown timestamp."""
        return f'cooldown_{symbol}'

    @staticmethod
    def _armed_key(basket_id: str) -> str:
        """State key for a basket's profit-protection armed flag."""
        return f'profit_armed_{basket_id}'

    def _cooldown_remaining(self, symbol: str) -> float:
        """Seconds of same-symbol cooldown still in effect (0 if none).

        Reads the persisted close timestamp from the (account-isolated) state
        store, so the cooldown survives restarts.
        """
        window = self.settings.symbol_cooldown_seconds
        if window <= 0:
            return 0.0
        raw = self.database.get_state(self._cooldown_key(symbol))
        if not raw:
            return 0.0
        try:
            closed_at = float(raw)
        except (TypeError, ValueError):
            return 0.0
        remaining = window - (time.time() - closed_at)
        return remaining if remaining > 0 else 0.0

    def _finalize_closed_state(self, symbol: str, basket_id: str) -> None:
        """Persist post-close state when a basket fully closes.

        Starts the same-symbol cooldown (regardless of exit reason) and clears
        the basket's profit-protection armed flag.
        """
        try:
            self.database.set_state(self._cooldown_key(symbol), str(time.time()))
        except Exception as e:
            logger.error('Failed to start cooldown for %s: %s', symbol, e)
        try:
            self.database.set_state(self._armed_key(basket_id), '')
        except Exception:
            pass

    def _finalize_closed_basket(self, basket: Basket) -> None:
        """Mark a basket closed in memory + DB and persist post-close state.

        Used when a basket empties out through individual-layer closes (the
        exchange orders were already placed per layer), so no aggregate close
        order is needed here.
        """
        basket.status = 'closed'
        self.database.close_basket(basket.id)
        self._finalize_closed_state(basket.symbol, basket.id)

    @staticmethod
    def _is_benign_close_error(exc: Exception) -> bool:
        """True if a close error means the position is already flat.

        These errors are safe to treat as a completed close (the exit already
        happened) rather than retrying or leaving the basket stale.
        """
        msg = str(exc).lower()
        benign = (
            'reduceonly', 'reduce only', 'reduce-only',
            'position not exist', 'no position', 'position does not exist',
            'position side does not match', 'order would not reduce',
            'quantity less than', 'unknown order sent',
        )
        return any(token in msg for token in benign)

    # ───────────────────────────────────────────
    # Active-Basket Reconciliation
    # ───────────────────────────────────────────

    def reconcile_baskets(self, baskets: List[Basket]) -> List[Basket]:
        """Close DB baskets that no longer have a live exchange position.

        Prevents stale active baskets: if the exchange reports no open position
        for a basket's symbol/side, the basket's exit already happened (manual
        close, liquidation, or a prior partial-close race) so we finalize it in
        the DB and start the same-symbol cooldown.

        Freshly opened baskets (< 60s old) are skipped to avoid races with the
        exchange's position propagation.

        Args:
            baskets: Active baskets for the account.

        Returns:
            The subset of baskets that remain genuinely active.
        """
        if not baskets:
            return baskets

        try:
            positions = self.exchange.fetch_positions()
        except Exception as e:
            logger.debug('Reconcile skipped (fetch_positions failed): %s', e)
            return baskets

        live = {
            (p.get('symbol'), (p.get('side') or '').lower())
            for p in positions
            if float(p.get('contracts', 0) or 0) > 0
        }

        now = time.time()
        still_active: List[Basket] = []
        for basket in baskets:
            if basket.status != 'active':
                continue
            if now - basket.created_at < 60:
                still_active.append(basket)
                continue
            if (basket.symbol, basket.side.lower()) in live:
                still_active.append(basket)
                continue
            logger.warning(
                'RECONCILE | basket %s (%s %s) has no live exchange position — '
                'finalizing as closed to avoid a stale active basket.',
                basket.id[:8], basket.side.upper(), basket.symbol,
            )
            self._finalize_closed_basket(basket)

        return still_active

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

        # ── Per-basket hard margin cap ──
        # The total margin across ALL layers of a single basket may never exceed
        # the fixed $5 basket cap (max_basket_margin_usd). This is the primary
        # guard that keeps combined Layer 1–4 margin at or below $5 for every
        # account, and is what enforces the $5 ceiling after all recovery layers.
        hard_cap = self.settings.get_margin_hard_cap(balance)
        projected_basket_margin = basket.total_margin + layer_params.margin
        if projected_basket_margin > hard_cap:
            logger.info(
                'Recovery L%d blocked for %s: basket margin %.2f + %.2f = %.2f '
                'would exceed hard cap %.2f (balance=%.2f)',
                layer_number, basket.symbol, basket.total_margin,
                layer_params.margin, projected_basket_margin, hard_cap, balance,
            )
            return

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
