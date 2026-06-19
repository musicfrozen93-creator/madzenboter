"""
ZenGrid — Position Manager (Dark-Venus basket recovery).

Orchestrates the full lifecycle of a recovery basket for ONE account:

  • open_position   — open Layer 1 on an approved entry signal (fixed sizing)
  • manage_baskets  — enforce the daily loss limit, take basket profit, and
                      activate the single recovery layer when ATR-spacing is hit
  • close_basket    — close an entire basket together (idempotent, reduce-only)

Hard rules enforced here:
  • Only the supported (correlated) symbols are traded.
  • Per-tier max active symbols (Tier 1: 2, Tier 2: 3) and max positions.
  • At most ONE basket per symbol.
  • At most 2 layers per basket (Layer 1 + ONE recovery) — never a martingale.
  • Correlation protection: a new correlated basket needs a stronger signal score
    the more baskets are already open (0 → score>=2, 1+ → score>=3).
  • Account death protection: equity below the tier floor PERMANENTLY locks the
    account and closes all baskets. Daily loss closes ALL baskets; daily profit
    blocks NEW entries.

Every entry/skip/recovery/close is logged with the account id, tier, symbol,
direction, entry price, recovery layer, margin, basket PnL, and the reason.
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
        """Open a new basket (Layer 1) from an approved entry signal.

        Entry validation runs in a strict order (all PER-ACCOUNT, never global):
          1. account lock status (emergency)        ┐ can_take_new_entry()
          2. daily profit limit                     │  (latches refreshed from
          3. daily loss limit                       ┘  realised PnL first)
          4. cooldown status
          (5. BTC filter and 6. signal validity already ran upstream in the
           SignalEngine before this approved signal was fanned out)
          7. structural limits + exchange-safety sizing, then execute.
        """
        symbol = signal.symbol

        # ── BOT_CONTROL gate (admin control plane — separate from risk locks) ──
        if self.bot_control and not self.bot_control.can_open_trades():
            self._skip(symbol, signal.side, 'bot_control_disabled')
            return None

        # ── Supported symbol only (safety: never trade unsupported symbols) ──
        if not self.settings.is_supported_symbol(symbol):
            self._skip(symbol, signal.side, 'unsupported_symbol')
            return None

        # ── Account tier (balance ONLY selects the tier) ──
        tier = self.settings.get_tier(balance)
        if tier is None:
            self._skip(
                symbol, signal.side,
                f'balance_below_min_tier (balance={balance:.2f} < '
                f'{self.settings.min_tier_balance:.2f} USDT)',
            )
            return None

        # ── [1] lock status + [2] daily profit + [3] daily loss (PER-ACCOUNT) ──
        # Refresh the latches from realised PnL / wallet equity first, so a
        # realised breach is caught even when the management loop hasn't run
        # (e.g. all baskets are already closed). Floating PnL is handled by
        # manage_baskets; passing 0 here can only fail to latch, never falsely
        # latch. These locks are this account's alone — never global.
        self.risk_manager.check_account_death_protection(balance, tier)
        self.risk_manager.check_loss_limit(0.0, tier)
        self.risk_manager.update_profit_target(0.0, tier)
        allowed, reason = self.risk_manager.can_take_new_entry()
        if not allowed:
            self._skip(symbol, signal.side, reason, tier=tier['id'])
            return None

        # ── [4] Same-symbol cooldown after a recent close (PER-ACCOUNT) ──
        remaining_cd = self._cooldown_remaining(symbol)
        if remaining_cd > 0:
            self._skip(
                symbol, signal.side, f'cooldown ({remaining_cd:.0f}s remaining)', tier=tier['id']
            )
            return None

        # ── Structural limits (PER-TIER: max active symbols, max positions) ──
        active_baskets = self.database.load_active_baskets()
        if any(b.symbol == symbol for b in active_baskets):
            self._skip(symbol, signal.side, 'existing_basket_on_symbol', tier=tier['id'])
            return None
        max_symbols = tier['max_active_symbols']
        if len(active_baskets) >= max_symbols:
            self._skip(
                symbol, signal.side,
                f'max_active_symbols ({len(active_baskets)}/{max_symbols})',
                tier=tier['id'],
            )
            return None
        open_positions = sum(b.layer_count for b in active_baskets)
        if open_positions >= tier['max_positions']:
            self._skip(
                symbol, signal.side,
                f'max_positions ({open_positions}/{tier["max_positions"]})',
                tier=tier['id'],
            )
            return None

        # ── Correlation protection (TRX/XRP/XLM are correlated) ──
        # A new correlated basket needs a stronger signal the more baskets are
        # already open: 0 active → score >= 2, 1+ active → score >= 3.
        required_score = (
            self.settings.correlation_min_score_first if not active_baskets
            else self.settings.correlation_min_score_additional
        )
        if signal.strength_score < required_score:
            self._skip(
                symbol, signal.side,
                f'correlation_protection (score {signal.strength_score} < required '
                f'{required_score} with {len(active_baskets)} active basket(s))',
                tier=tier['id'],
            )
            return None

        leverage = self.settings.leverage

        try:
            market_info = self.exchange.get_symbol_info(symbol)
        except Exception as e:
            self._skip(symbol, signal.side, f'market_info_error ({e})', tier=tier['id'])
            return None

        # FIXED Layer-1 margin from the tier (never balance-scaled).
        margin = tier['layer1_margin']
        plan = self.position_sizer.build_order(
            margin, signal.current_price, leverage, market_info
        )
        if not plan['suitable']:
            self._skip(symbol, signal.side, f"sizing_unsuitable ({plan['reason']})", tier=tier['id'])
            return None

        quantity = plan['quantity']   # planned qty; actual fill is resolved below

        # ── Execute ──
        try:
            self.exchange.set_margin_mode(symbol, 'cross')
            self.exchange.set_leverage(symbol, leverage)

            order_side = 'buy' if signal.side == 'long' else 'sell'
            order = self.exchange.place_market_order(symbol, order_side, quantity)

            # ── Partial-fill handling — NEVER assume a full fill ──
            # Use the ACTUAL filled quantity and recompute the ACTUAL margin so
            # basket TP and exposure are derived from what really filled.
            fill = self._resolve_fill(order, quantity, signal.current_price, leverage)
            if fill is None:
                self._skip(symbol, signal.side, 'no_fill (order returned 0 filled)', tier=tier['id'])
                return None
            filled_qty, fill_price, actual_margin = fill
            if filled_qty + 1e-12 < quantity:
                logger.warning(
                    'PARTIAL_FILL | account=%s symbol=%s requested=%.8f filled=%.8f '
                    'actual_margin=%.4f — basket sized to the actual fill.',
                    self.account_id, symbol, quantity, filled_qty, actual_margin,
                    extra=self._log_extra,
                )

            layer = RecoveryLayer(
                layer_number=1,
                entry_price=fill_price,
                margin=actual_margin,
                quantity=filled_qty,
                side=signal.side,
            )
            # The tier is LOCKED onto the basket (stored in the volatility column)
            # so recovery margin, exposure cap, and TP target never change if the
            # account balance later crosses a tier boundary.
            basket = Basket(
                symbol=symbol,
                side=signal.side,
                atr_at_entry=signal.atr,
                volatility=tier['id'],
                leverage=leverage,
                account_id=self.account_id,
            )
            basket.add_layer(layer)
            self.database.save_basket(basket)

            trade_logger.info(
                'OPEN | account=%s tier=%s symbol=%s direction=%s layer=1 entry=%.6f '
                'qty=%.8f margin=%.4f lev=%dx btc=%s basket_pnl=0.0000 | reason: %s',
                self.account_id, tier['id'], symbol, signal.side.upper(), fill_price,
                filled_qty, actual_margin, leverage, (signal.market_regime or 'unknown').upper(),
                signal.reason or 'entry signal', extra=self._log_extra,
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
          0. Account death protection → equity below the tier floor permanently
             PROTECTION_LOCKS the account and closes ALL baskets.
          1. Daily loss limit  → close ALL baskets, stop for the day.
          2. Basket take-profit → close the basket at its USDT target.
          3. Recovery layer     → activate Layer 2 when ATR-spacing is hit.
        The daily profit target latches the new-entry lock (no closing).
        """
        active = [b for b in baskets if b.status == 'active' and b.layer_count > 0]
        if not active:
            return []

        # Account tier for daily limits — balance ONLY selects the tier; falls
        # back to the most conservative tier if balance dipped below the minimum.
        tier = self.settings.get_tier_or_default(balance)

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

        # ── PRIORITY 0: account death protection (PERMANENT lock) ──
        # Equity = wallet balance + open floating PnL (the account's real value).
        # If it falls below the tier floor ($15 Tier 1 / $30 Tier 2) the account
        # is PROTECTION_LOCKED permanently (admin reset only) and every basket is
        # closed immediately. This is survival-first and outranks all else.
        equity = balance + total_unrealized
        if self.risk_manager.check_account_death_protection(equity, tier):
            self.close_all_baskets(active, 'protection_lock')
            return []

        # ── PRIORITY 1: daily loss limit (close ALL baskets + recovery layers) ──
        # Uses realised + unrealised trading PnL (never wallet balance) so it
        # fires before losses are realised, regardless of deposits/withdrawals.
        if self.risk_manager.check_loss_limit(total_unrealized, tier):
            self.close_all_baskets(active, 'daily_loss_limit')
            return []

        # Latch the daily profit target lock (blocks new entries only).
        try:
            self.risk_manager.update_profit_target(total_unrealized, tier)
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

        # The basket's LOCKED tier drives recovery margin and the exposure cap.
        tier = self.settings.get_tier_by_id(basket.volatility) or self.settings.account_tiers[0]
        margin = tier['layer2_margin']

        # Never exceed the tier's maximum basket exposure.
        projected = basket.total_margin + margin
        if projected > tier['max_basket_exposure'] + 1e-9:
            logger.info(
                'RECOVERY_SKIP | account=%s tier=%s symbol=%s reason=exposure_cap '
                '(%.2f + %.2f = %.2f > %.2f)',
                self.account_id, tier['id'], basket.symbol, basket.total_margin,
                margin, projected, tier['max_basket_exposure'], extra=self._log_extra,
            )
            return

        leverage = basket.leverage
        try:
            market_info = self.exchange.get_symbol_info(basket.symbol)
        except Exception as e:
            logger.error('Recovery market_info failed for %s: %s', basket.symbol, e)
            return

        plan = self.position_sizer.build_order(
            margin, current_price, leverage, market_info
        )
        if not plan['suitable']:
            logger.warning(
                'RECOVERY_SKIP | account=%s tier=%s symbol=%s reason=sizing_unsuitable (%s)',
                self.account_id, tier['id'], basket.symbol, plan['reason'],
                extra=self._log_extra,
            )
            return

        try:
            order_side = 'buy' if basket.side == 'long' else 'sell'
            order = self.exchange.place_market_order(
                basket.symbol, order_side, plan['quantity']
            )

            # ── Partial-fill handling — use the ACTUAL filled qty/margin ──
            fill = self._resolve_fill(order, plan['quantity'], current_price, leverage)
            if fill is None:
                logger.warning(
                    'RECOVERY_SKIP | account=%s tier=%s symbol=%s reason=no_fill',
                    self.account_id, tier['id'], basket.symbol, extra=self._log_extra,
                )
                return
            filled_qty, fill_price, actual_margin = fill
            if filled_qty + 1e-12 < plan['quantity']:
                logger.warning(
                    'PARTIAL_FILL | account=%s symbol=%s layer=%d requested=%.8f filled=%.8f '
                    'actual_margin=%.4f — recovery layer sized to the actual fill.',
                    self.account_id, basket.symbol, next_layer, plan['quantity'],
                    filled_qty, actual_margin, extra=self._log_extra,
                )

            layer = self.recovery.build_layer(
                basket, next_layer, actual_margin, filled_qty, fill_price
            )
            basket.add_layer(layer)
            self.database.update_basket(basket)

            # Basket TP + exposure are recomputed from the ACTUAL layer qty/margin.
            basket_pnl = basket.unrealized_pnl(current_price)
            trade_logger.info(
                'RECOVERY | account=%s tier=%s symbol=%s direction=%s layer=%d entry=%.6f '
                'qty=%.8f margin=%.4f basket_pnl=%.4f exposure=%.4f | reason: Layer 1 '
                'drawdown exceeded ATR×%.1f',
                self.account_id, tier['id'], basket.symbol, basket.side.upper(), next_layer,
                fill_price, filled_qty, actual_margin, basket_pnl, basket.total_margin,
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
            daily_realized = self.risk_manager.daily_realized_pnl()
            trade_logger.info(
                'CLOSE | account=%s tier=%s symbol=%s direction=%s layers=%d entry=%.6f '
                'exit=%.6f basket_pnl=%s%.4f USDT daily_realized=%.4f cooldown=%dm | reason: %s',
                self.account_id, basket.volatility, basket.symbol, basket.side.upper(),
                trade.layers_used, trade.entry_price, current_price, sign, realized_pnl,
                daily_realized, self.settings.symbol_cooldown_seconds // 60, reason,
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

    def _skip(self, symbol: str, side: str, reason: str, tier: str = '-') -> None:
        """Log a skipped entry with the required fields."""
        logger.info(
            'ENTRY_SKIP | account=%s tier=%s symbol=%s direction=%s reason=%s',
            self.account_id, tier, symbol, (side or '').upper(), reason,
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

    @staticmethod
    def _resolve_fill(order: dict, requested_qty: float, fallback_price: float, leverage: int):
        """Extract the ACTUAL fill from an order — never assume a full fill.

        Reads the truly-filled quantity (ccxt ``filled``, falling back to
        ``amount``) and the average fill price, then derives the ACTUAL margin
        consumed (filled × price / leverage). Basket TP and exposure are computed
        from these actual values, so a partial fill is handled correctly.

        Returns:
            Tuple (filled_qty, fill_price, actual_margin), or None if nothing
            filled (filled quantity <= 0).
        """
        fill_price = float(
            order.get('average', order.get('price', fallback_price)) or fallback_price
        )
        if fill_price <= 0:
            fill_price = fallback_price

        raw = order.get('filled')
        if raw is None:
            raw = order.get('amount')
        try:
            filled = float(raw) if raw is not None else float(requested_qty)
        except (TypeError, ValueError):
            filled = float(requested_qty)

        if filled <= 0:
            return None

        actual_margin = (filled * fill_price / leverage) if leverage > 0 else 0.0
        return filled, fill_price, round(actual_margin, 6)
