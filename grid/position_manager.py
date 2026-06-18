"""
ZenGrid — Position Manager (Dark-Venus basket recovery).

Orchestrates the full lifecycle of a recovery basket for ONE account:

  • open_position   — open Layer 1 on an approved entry signal (fixed sizing)
  • manage_baskets  — enforce the daily loss limit, take basket profit, and
                      activate the single recovery layer when ATR-spacing is hit
  • close_basket    — close an entire basket together (idempotent, reduce-only)

Hard rules enforced here:
  • Only the supported symbols are traded.
  • At most max_baskets_per_account simultaneous baskets (default 2).
  • At most ONE basket per symbol.
  • At most 2 layers per basket (Layer 1 + ONE recovery) — never a martingale.
  • Daily loss limit closes ALL baskets; daily profit target blocks NEW entries.

Every entry/skip/recovery/close is logged with the account id, symbol, direction,
entry price, recovery layer, and the reason.
"""

import logging
import threading
import time
from typing import List, Optional

from config.settings import Settings
from core.dto import Basket, RecoveryLayer, Signal, TradeRecord
from exchange.client import ExchangeClient
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager

logger = logging.getLogger(__name__)
trade_logger = logging.getLogger('trades')


class PositionManager:
    """Manages the full lifecycle of recovery baskets for a single account."""

    def __init__(
        self,
        exchange_client: ExchangeClient,
        settings: Settings,
        database,
        risk_manager: RiskManager,
        position_sizer: PositionSizer,
        recovery_system: RecoverySystem,
        tp_manager: TakeProfitManager,
        bot_control=None,
    ) -> None:
        self.exchange = exchange_client
        self.settings = settings
        self.database = database
        self.risk_manager = risk_manager
        self.position_sizer = position_sizer
        self.recovery = recovery_system
        self.tp_manager = tp_manager
        self.bot_control = bot_control
        # Account id from the isolated DB wrapper (for log enrichment).
        self.account_id = getattr(database, '_account_id', None)

        # Idempotent-close guard.
        self._closing_lock = threading.Lock()
        self._closing: set = set()

    @property
    def _log_extra(self) -> dict:
        return {'account_id': self.account_id if self.account_id is not None else 'SYSTEM'}

    # ───────────────────────────────────────────
    # Open position (Layer 1)
    # ───────────────────────────────────────────

    def open_position(self, signal: Signal, balance: float) -> Optional[Basket]:
        """Open a new basket (Layer 1) from an approved entry signal."""
        symbol = signal.symbol

        # ── BOT_CONTROL gate ──
        if self.bot_control and not self.bot_control.can_open_trades():
            self._skip(symbol, signal.side, 'bot_control_disabled')
            return None

        # ── Supported symbol only (safety: never trade unsupported symbols) ──
        if not self.settings.is_supported_symbol(symbol):
            self._skip(symbol, signal.side, 'unsupported_symbol')
            return None

        # ── Daily profit/loss locks (block NEW entries only) ──
        allowed, reason = self.risk_manager.can_take_new_entry()
        if not allowed:
            self._skip(symbol, signal.side, reason)
            return None

        active_baskets = self.database.load_active_baskets()

        # ── One basket per symbol ──
        if any(b.symbol == symbol for b in active_baskets):
            self._skip(symbol, signal.side, 'existing_basket_on_symbol')
            return None

        # ── Max simultaneous baskets ──
        if len(active_baskets) >= self.settings.max_baskets_per_account:
            self._skip(
                symbol, signal.side,
                f'max_baskets_reached ({len(active_baskets)}/{self.settings.max_baskets_per_account})',
            )
            return None

        # ── Same-symbol cooldown after a recent close ──
        remaining_cd = self._cooldown_remaining(symbol)
        if remaining_cd > 0:
            self._skip(symbol, signal.side, f'cooldown ({remaining_cd:.0f}s remaining)')
            return None

        leverage = self.settings.leverage

        try:
            market_info = self.exchange.get_symbol_info(symbol)
        except Exception as e:
            self._skip(symbol, signal.side, f'market_info_error ({e})')
            return None

        plan = self.position_sizer.build_order(
            1, signal.current_price, leverage, market_info
        )
        if not plan['suitable']:
            self._skip(symbol, signal.side, f"sizing_unsuitable ({plan['reason']})")
            return None

        quantity = plan['quantity']
        margin = plan['margin']

        # ── Execute ──
        try:
            self.exchange.set_margin_mode(symbol, 'cross')
            self.exchange.set_leverage(symbol, leverage)

            order_side = 'buy' if signal.side == 'long' else 'sell'
            order = self.exchange.place_market_order(symbol, order_side, quantity)
            fill_price = float(
                order.get('average', order.get('price', signal.current_price))
                or signal.current_price
            )

            layer = RecoveryLayer(
                layer_number=1,
                entry_price=fill_price,
                margin=margin,
                quantity=quantity,
                side=signal.side,
            )
            basket = Basket(
                symbol=symbol,
                side=signal.side,
                atr_at_entry=signal.atr,
                volatility=signal.volatility,
                leverage=leverage,
                account_id=self.account_id,
            )
            basket.add_layer(layer)
            self.database.save_basket(basket)

            trade_logger.info(
                'OPEN | account=%s symbol=%s direction=%s layer=1 entry=%.6f '
                'qty=%.8f margin=%.4f lev=%dx | reason: %s',
                self.account_id, symbol, signal.side.upper(), fill_price,
                quantity, margin, leverage, signal.reason or 'entry signal',
                extra=self._log_extra,
            )
            logger.info(
                'POSITION_OPEN | account=%s symbol=%s direction=%s entry=%.6f basket=%s',
                self.account_id, symbol, signal.side.upper(), fill_price, basket.id[:8],
                extra=self._log_extra,
            )
            return basket

        except Exception as e:
            self._skip(symbol, signal.side, f'order_error ({e})')
            return None

    # ───────────────────────────────────────────
    # Manage baskets
    # ───────────────────────────────────────────

    def manage_baskets(self, baskets: List[Basket], balance: float) -> List[Basket]:
        """Manage all active baskets for the account.

        Order of priority (survival first):
          1. Daily loss limit  → close ALL baskets, stop for the day.
          2. Basket take-profit → close the basket at its USDT target.
          3. Recovery layer     → activate Layer 2 when ATR-spacing is hit.
        The daily profit target latches the new-entry lock (no closing).
        """
        active = [b for b in baskets if b.status == 'active' and b.layer_count > 0]
        if not active:
            return []

        # ── Price snapshot + total open unrealised PnL ──
        prices: dict = {}
        total_unrealized = 0.0
        for basket in active:
            if basket.symbol in prices:
                continue
            try:
                ticker = self.exchange.fetch_ticker(basket.symbol)
                price = float(ticker['last'])
                if price > 0:
                    prices[basket.symbol] = price
            except Exception as e:
                logger.debug('Ticker fetch failed for %s: %s', basket.symbol, e)
        for basket in active:
            price = prices.get(basket.symbol)
            if price:
                total_unrealized += basket.unrealized_pnl(price)

        # ── PRIORITY 1: daily loss limit (close everything) ──
        if self.risk_manager.check_loss_limit(total_unrealized):
            self.close_all_baskets(active, 'daily_loss_limit')
            return []

        # Latch the daily profit target lock (blocks new entries only).
        try:
            self.risk_manager.update_profit_target()
        except Exception as e:
            logger.debug('update_profit_target failed: %s', e)

        remaining: List[Basket] = []
        for basket in active:
            price = prices.get(basket.symbol)
            if not price:
                remaining.append(basket)
                continue
            try:
                # ── PRIORITY 2: basket take-profit ──
                if self.tp_manager.check_basket_tp(basket, price):
                    self.close_basket(basket, 'basket_tp')
                    continue

                # ── PRIORITY 3: single recovery layer ──
                next_layer = self.recovery.check_recovery_trigger(
                    basket, price, basket.atr_at_entry
                )
                if next_layer is not None:
                    self._add_recovery_layer(basket, price)

                self.database.update_basket(basket)
                remaining.append(basket)
            except Exception as e:
                logger.error(
                    'Error managing basket %s (%s): %s',
                    basket.id[:8], basket.symbol, e, extra=self._log_extra,
                )
                remaining.append(basket)

        return remaining

    # ───────────────────────────────────────────
    # Recovery layer
    # ───────────────────────────────────────────

    def _add_recovery_layer(self, basket: Basket, current_price: float) -> None:
        """Add the single recovery layer (Layer 2) to a basket."""
        # BOT_CONTROL gate (recovery layers are new exchange orders).
        if self.bot_control and not self.bot_control.can_add_recovery_layer():
            logger.info(
                '[CONTROL] Recovery layer blocked for %s', basket.symbol,
                extra=self._log_extra,
            )
            return

        next_layer = basket.layer_count + 1
        if next_layer > self.settings.recovery_max_layers:
            return  # never a Layer 3+

        leverage = basket.leverage
        try:
            market_info = self.exchange.get_symbol_info(basket.symbol)
        except Exception as e:
            logger.error('Recovery market_info failed for %s: %s', basket.symbol, e)
            return

        plan = self.position_sizer.build_order(
            next_layer, current_price, leverage, market_info
        )
        if not plan['suitable']:
            logger.warning(
                'Recovery L%d unsuitable for %s: %s',
                next_layer, basket.symbol, plan['reason'], extra=self._log_extra,
            )
            return

        try:
            order_side = 'buy' if basket.side == 'long' else 'sell'
            order = self.exchange.place_market_order(
                basket.symbol, order_side, plan['quantity']
            )
            fill_price = float(
                order.get('average', order.get('price', current_price)) or current_price
            )
            layer = self.recovery.build_layer(
                basket, next_layer, plan['margin'], plan['quantity'], fill_price
            )
            basket.add_layer(layer)
            self.database.update_basket(basket)

            trade_logger.info(
                'RECOVERY | account=%s symbol=%s direction=%s layer=%d entry=%.6f '
                'qty=%.8f margin=%.4f | reason: Layer 1 drawdown exceeded ATR×%.1f',
                self.account_id, basket.symbol, basket.side.upper(), next_layer,
                fill_price, plan['quantity'], plan['margin'],
                self.settings.layer2_atr_multiplier, extra=self._log_extra,
            )
        except Exception as e:
            logger.error(
                'Failed to add recovery L%d for %s: %s',
                next_layer, basket.symbol, e, extra=self._log_extra,
            )

    # ───────────────────────────────────────────
    # Close operations
    # ───────────────────────────────────────────

    def close_basket(self, basket: Basket, reason: str) -> Optional[TradeRecord]:
        """Close an entire basket — all active layers together (idempotent)."""
        with self._closing_lock:
            if basket.status != 'active' or basket.id in self._closing:
                return None
            self._closing.add(basket.id)
            basket.status = 'closing'

        try:
            total_qty = basket.total_quantity
            if total_qty <= 0:
                basket.close_all()
                self.database.close_basket(basket.id)
                self._finalize_closed_state(basket.symbol, basket.id)
                return None

            ticker = self.exchange.fetch_ticker(basket.symbol)
            current_price = float(ticker['last'])

            for attempt in range(3):
                try:
                    self.exchange.close_position(basket.symbol, basket.side, total_qty)
                    break
                except Exception as e:
                    if self._is_benign_close_error(e):
                        logger.warning(
                            'Close for %s reports already flat (%s) — finalizing %s.',
                            basket.symbol, e, basket.id[:8], extra=self._log_extra,
                        )
                        break
                    if attempt == 2:
                        logger.critical(
                            'FAILED to close basket %s after 3 attempts: %s',
                            basket.id[:8], e, extra=self._log_extra,
                        )
                        return None
                    time.sleep(0.25)

            gross = basket.unrealized_pnl(current_price)
            fee = total_qty * current_price * self.settings.taker_fee_pct * 2
            realized_pnl = gross - fee

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
                account_id=self.account_id,
            )

            basket.close_all()
            self.database.close_basket(basket.id)
            self.database.save_trade(trade)
            self._finalize_closed_state(basket.symbol, basket.id)

            sign = '+' if realized_pnl >= 0 else ''
            trade_logger.info(
                'CLOSE | account=%s symbol=%s direction=%s layers=%d entry=%.6f '
                'exit=%.6f pnl=%s%.4f USDT | reason: %s',
                self.account_id, basket.symbol, basket.side.upper(), trade.layers_used,
                trade.entry_price, current_price, sign, realized_pnl, reason,
                extra=self._log_extra,
            )
            return trade

        except Exception as e:
            logger.error(
                'Error closing basket %s: %s', basket.id[:8], e, extra=self._log_extra,
            )
            return None
        finally:
            with self._closing_lock:
                self._closing.discard(basket.id)

    def close_all_baskets(self, baskets: List[Basket], reason: str) -> List[TradeRecord]:
        """Close every active basket (e.g. daily loss limit, force-close)."""
        trades: List[TradeRecord] = []
        for basket in baskets:
            if basket.status == 'active':
                trade = self.close_basket(basket, reason)
                if trade:
                    trades.append(trade)
        return trades

    # ───────────────────────────────────────────
    # Reconciliation
    # ───────────────────────────────────────────

    def reconcile_baskets(self, baskets: List[Basket]) -> List[Basket]:
        """Finalize DB baskets that no longer have a live exchange position."""
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
                'RECONCILE | basket %s (%s %s) has no live position — finalizing.',
                basket.id[:8], basket.side.upper(), basket.symbol, extra=self._log_extra,
            )
            basket.status = 'closed'
            self.database.close_basket(basket.id)
            self._finalize_closed_state(basket.symbol, basket.id)

        return still_active

    # ───────────────────────────────────────────
    # Cooldown + helpers
    # ───────────────────────────────────────────

    @staticmethod
    def _cooldown_key(symbol: str) -> str:
        return f'cooldown_{symbol}'

    def _cooldown_remaining(self, symbol: str) -> float:
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
        try:
            self.database.set_state(self._cooldown_key(symbol), str(time.time()))
        except Exception as e:
            logger.error('Failed to start cooldown for %s: %s', symbol, e)

    def _skip(self, symbol: str, side: str, reason: str) -> None:
        """Log a skipped entry with the required fields."""
        logger.info(
            'ENTRY_SKIP | account=%s symbol=%s direction=%s reason=%s',
            self.account_id, symbol, (side or '').upper(), reason,
            extra=self._log_extra,
        )

    @staticmethod
    def _is_benign_close_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        benign = (
            'reduceonly', 'reduce only', 'reduce-only',
            'position not exist', 'no position', 'position does not exist',
            'position side does not match', 'order would not reduce',
            'quantity less than', 'unknown order sent',
        )
        return any(token in msg for token in benign)
