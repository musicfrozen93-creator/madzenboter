"""
ZenGrid — Position Manager (single-entry scalping).

Orchestrates the full lifecycle of ONE position per symbol for ONE account:

  • open_position   — open a single position on an approved entry signal
  • manage_baskets  — enforce the daily loss limit, take profit, and cut the
                      stop-loss
  • close_basket    — close the position (idempotent, reduce-only)

Hard rules enforced here:
  • Only the supported (fixed-universe) symbols are traded.
  • Per-tier max active symbols / max positions (Tier 1: 8, Tier 2: 10).
  • At most ONE position per symbol — SINGLE ENTRY ONLY.
  • NO recovery, NO Layer 2, NO averaging down, NO martingale, NO grid expansion.
  • Fixed take-profit (tp_margin_pct × margin) and stop-loss (sl_margin_pct ×
    margin) on every position.
  • Account death protection: equity below the tier floor PERMANENTLY locks the
    account and closes all positions. Daily loss closes ALL positions; daily
    profit blocks NEW entries.

A position is persisted as a basket holding exactly one layer (the storage model
is retained so the shared database schema is unchanged). Every entry/skip/close
is logged with the account id, tier, symbol, direction, entry price, margin,
PnL, and the reason.
"""

import logging
import threading
import time
from typing import List, Optional

from config.settings import Settings
from core.dto import Basket, RecoveryLayer, Signal, TradeRecord
from exchange.client import ExchangeClient
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager

logger = logging.getLogger(__name__)
trade_logger = logging.getLogger('trades')


class PositionManager:
    """Manages the full lifecycle of single-entry positions for one account."""

    def __init__(
        self,
        exchange_client: ExchangeClient,
        settings: Settings,
        database,
        risk_manager: RiskManager,
        position_sizer: PositionSizer,
        tp_manager: TakeProfitManager,
        bot_control=None,
    ) -> None:
        self.exchange = exchange_client
        self.settings = settings
        self.database = database
        self.risk_manager = risk_manager
        self.position_sizer = position_sizer
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
    # Open position (single entry)
    # ───────────────────────────────────────────

    def open_position(self, signal: Signal, balance: float) -> Optional[Basket]:
        """Open a new single-entry position from an approved entry signal.

        Entry validation runs in a strict order (all PER-ACCOUNT, never global):
          1. account lock status (emergency)        ┐ can_take_new_entry()
          2. daily profit limit                     │  (latches refreshed from
          3. daily loss limit                       ┘  realised PnL first)
          4. cooldown status
          (5. BTC filter and 6. signal validity already ran upstream in the
           SignalEngine before this approved signal was fanned out)
          7. structural limits + signal quality + exchange-safety sizing, then
             execute.
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
        # (e.g. all positions are already closed). Floating PnL is handled by
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
            self._skip(symbol, signal.side, 'existing_position_on_symbol', tier=tier['id'])
            return None
        max_symbols = tier['max_active_symbols']
        if len(active_baskets) >= max_symbols:
            self._skip(
                symbol, signal.side,
                f'max_active_symbols ({len(active_baskets)}/{max_symbols})',
                tier=tier['id'],
            )
            return None
        open_positions = len(active_baskets)   # single entry → one position each
        if open_positions >= tier['max_positions']:
            self._skip(
                symbol, signal.side,
                f'max_positions ({open_positions}/{tier["max_positions"]})',
                tier=tier['id'],
            )
            return None

        # ── Signal quality gate (single-threshold score, replaces correlation) ──
        if signal.strength_score < self.settings.min_signal_score:
            self._skip(
                symbol, signal.side,
                f'low_signal_score (score {signal.strength_score} < required '
                f'{self.settings.min_signal_score})',
                tier=tier['id'],
            )
            return None

        leverage = self.settings.leverage

        try:
            market_info = self.exchange.get_symbol_info(symbol)
        except Exception as e:
            self._skip(symbol, signal.side, f'market_info_error ({e})', tier=tier['id'])
            return None

        # Size at a FRESH execution-time price (not the possibly-stale signal
        # price) so the quantity matches the real fill and the recorded margin
        # stays close to the intended tier margin.
        try:
            exec_price = float(self.exchange.fetch_ticker(symbol)['last']) or signal.current_price
        except Exception:
            exec_price = signal.current_price
        if exec_price <= 0:
            exec_price = signal.current_price

        # FIXED margin from the tier (never balance-scaled).
        margin = tier['margin_per_trade']
        plan = self.position_sizer.build_order(
            margin, exec_price, leverage, market_info
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
            # the take-profit/stop-loss are derived from what really filled.
            fill = self._resolve_fill(order, quantity, exec_price, leverage)
            if fill is None:
                self._skip(symbol, signal.side, 'no_fill (order returned 0 filled)', tier=tier['id'])
                return None
            filled_qty, fill_price, actual_margin = fill
            if filled_qty + 1e-12 < quantity:
                logger.warning(
                    'PARTIAL_FILL | account=%s symbol=%s requested=%.8f filled=%.8f '
                    'actual_margin=%.4f — position sized to the actual fill.',
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
            # The tier is LOCKED onto the position (stored in the volatility
            # column) so the margin and TP/SL targets never change if the account
            # balance later crosses a tier boundary.
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
                'OPEN | account=%s tier=%s symbol=%s direction=%s entry=%.6f '
                'qty=%.8f margin=%.4f lev=%dx btc=%s pnl=0.0000 | reason: %s',
                self.account_id, tier['id'], symbol, signal.side.upper(), fill_price,
                filled_qty, actual_margin, leverage, (signal.market_regime or 'unknown').upper(),
                signal.reason or 'entry signal', extra=self._log_extra,
            )
            logger.info(
                'POSITION_OPEN | account=%s symbol=%s direction=%s entry=%.6f position=%s',
                self.account_id, symbol, signal.side.upper(), fill_price, basket.id[:8],
                extra=self._log_extra,
            )
            return basket

        except Exception as e:
            self._skip(symbol, signal.side, f'order_error ({e})')
            return None

    # ───────────────────────────────────────────
    # Manage positions
    # ───────────────────────────────────────────

    def manage_baskets(self, baskets: List[Basket], balance: float) -> List[Basket]:
        """Manage all active positions for the account.

        Order of priority (survival first):
          0. Account death protection → equity below the tier floor permanently
             PROTECTION_LOCKS the account and closes ALL positions.
          1. Daily loss limit  → close ALL positions, stop for the day.
          2a. TP LOCK (frozen)  → a position with a committed profit exit keeps
              being closed (price changes ignored) until it is flat.
          2b. Position exit     → net ≥ TP target → activate TP lock + close;
              net ≤ −SL target → close as 'sl'.
        The daily profit target latches the new-entry lock (no closing).
        """
        active = [b for b in baskets if b.status == 'active' and b.layer_count > 0]
        if not active:
            # No open positions → the portfolio trailing profit lock resets.
            try:
                self.risk_manager.reset_portfolio_profit_lock()
            except Exception as e:
                logger.debug('portfolio lock reset failed: %s', e)
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
        # is PROTECTION_LOCKED permanently (admin reset only) and every position
        # is closed immediately. This is survival-first and outranks all else.
        equity = balance + total_unrealized
        if self.risk_manager.check_account_death_protection(equity, tier):
            self.close_all_baskets(active, 'protection_lock')
            return []

        # ── PRIORITY 1: daily loss limit (close ALL positions) ──
        # Uses realised + unrealised trading PnL (never wallet balance) so it
        # fires before losses are realised, regardless of deposits/withdrawals.
        if self.risk_manager.check_loss_limit(total_unrealized, tier):
            self.close_all_baskets(active, 'daily_loss_limit')
            return []

        # ── PRIORITY 1.5: portfolio trailing profit lock (dynamic) ──
        # Per-account: arms once total open unrealised PnL reaches the tier
        # trigger ($0.50 T1 / $0.80 T2), trails the peak, and flattens ALL
        # positions the moment current profit drops below the dynamic protected
        # level max(floor, peak × band%) (protection % ratchets up 70→85% with the
        # peak). Independent of the daily profit lock; resets once positions close.
        if self.risk_manager.update_portfolio_profit_lock(total_unrealized, tier):
            self.close_all_baskets(active, 'portfolio_profit_lock')
            self.risk_manager.reset_portfolio_profit_lock()
            return []

        # Latch the daily profit target lock (blocks new entries only).
        try:
            self.risk_manager.update_profit_target(total_unrealized, tier)
        except Exception as e:
            logger.debug('update_profit_target failed: %s', e)

        remaining: List[Basket] = []
        for basket in active:
            price = prices.get(basket.symbol)

            # ── PRIORITY 2a: TP LOCK (frozen exit) ──
            # If a profit exit was already committed for this position, the exit
            # decision is FROZEN: ignore all later price changes and keep
            # attempting closure until the position is flat and the exchange
            # confirms. The lock is DB-persisted, so it survives a bot/process/
            # server restart or crash recovery — a position that hit its target
            # can never be left open by a post-target price reversal.
            locked_reason = self._tp_lock_reason(basket)
            if locked_reason:
                try:
                    if not self._execute_tp_locked_close(basket, locked_reason, price):
                        remaining.append(basket)  # still open — retry next cycle
                except Exception as e:
                    logger.error(
                        'TP-locked close failed for %s (%s): %s',
                        basket.id[:8], basket.symbol, e, extra=self._log_extra,
                    )
                    remaining.append(basket)
                continue

            if not price:
                # Ticker missing from the snapshot — RETRY before skipping so a
                # position that may be due to close is not deferred a whole cycle.
                price = self._fetch_price_with_retry(basket.symbol)
                if not price:
                    logger.warning(
                        'PRICE_UNAVAILABLE | account=%s symbol=%s — deferring position '
                        'after ticker retries.', self.account_id, basket.symbol,
                        extra=self._log_extra,
                    )
                    remaining.append(basket)
                    continue
                prices[basket.symbol] = price
            try:
                # ── PRIORITY 2b: position exit (TP target or hard SL) ──
                exit_reason, m = self.tp_manager.evaluate_exit(basket, price)

                # ── TP_DEBUG — full closure-decision trace ──
                # Emitted for any position in profit (the "stayed open" case) and
                # on every actual close, so the exact decision is auditable.
                if m['net_pnl'] > 0 or exit_reason:
                    trade_logger.info(
                        'TP_DEBUG | account=%s tier=%s symbol=%s gross_pnl=%.6f '
                        'net_pnl=%.6f fee=%.6f tp_target=%.4f sl_target=%.4f '
                        'roi=%.2f%% decision=%s',
                        self.account_id, basket.volatility, basket.symbol,
                        m['gross_pnl'], m['net_pnl'], m['fee'], m['tp_target'],
                        m['sl_target'], m['roi'] * 100, m['decision'],
                        extra=self._log_extra,
                    )

                # ── Take-profit → IMMEDIATE TP LOCK + close (same cycle) ──
                # The moment the TP condition is true we log TP_DETECTED, freeze
                # the exit with the TP lock, and submit the close order in THIS
                # cycle — no waiting, no TP re-evaluation. A post-target reversal
                # can never leave a profitable position open.
                if exit_reason == 'tp':
                    trade_logger.info(
                        'TP_DETECTED | account=%s symbol=%s pnl=%.4f target=%.4f timestamp=%.0f',
                        self.account_id, basket.symbol, m['net_pnl'], m['tp_target'],
                        time.time(), extra=self._log_extra,
                    )
                    self._activate_tp_lock(basket, exit_reason, m)
                    if not self._execute_tp_locked_close(basket, exit_reason, price):
                        remaining.append(basket)  # exchange busy — retry next cycle
                    continue

                # ── Hard stop-loss (net loss reached the per-position floor) ──
                if exit_reason == 'sl':
                    self._close_basket_sl(basket, m)
                    continue

                self.database.update_basket(basket)
                remaining.append(basket)
            except Exception as e:
                logger.error(
                    'Error managing position %s (%s): %s',
                    basket.id[:8], basket.symbol, e, extra=self._log_extra,
                )
                remaining.append(basket)

        return remaining

    # ───────────────────────────────────────────
    # TP lock (persistent exit-execution guarantee)
    # ───────────────────────────────────────────

    @staticmethod
    def _tp_lock_key(basket_id: str) -> str:
        return f'tp_lock_{basket_id}'

    def _tp_lock_reason(self, basket: Basket) -> Optional[str]:
        """Return the committed exit reason if this position is TP-locked, else None.

        Read from the account-isolated, DB-persisted state so the lock survives a
        bot/process/server restart and crash recovery.
        """
        try:
            reason = self.database.get_state(self._tp_lock_key(basket.id))
        except Exception as e:
            logger.debug('tp_lock read failed for %s: %s', basket.id[:8], e)
            return None
        return reason or None

    def _activate_tp_lock(self, basket: Basket, reason: str, m: dict) -> None:
        """Persist the TP lock and log TP_LOCK_ACTIVATED (idempotent).

        Once set, the position's exit decision is FROZEN — manage_baskets stops
        re-evaluating targets and only keeps attempting closure. Activation is a
        no-op if the lock is already set (so the activation log fires once).
        """
        key = self._tp_lock_key(basket.id)
        try:
            if self.database.get_state(key):
                return  # already locked — keep the original activation record
        except Exception:
            pass
        activation_time = time.time()
        try:
            self.database.set_state(key, reason)
            self.database.set_state(f'{key}_time', str(activation_time))
        except Exception as e:
            logger.error('Failed to persist TP lock for %s: %s', basket.id[:8], e)
        trade_logger.info(
            'TP_LOCK_ACTIVATED | account=%s symbol=%s pnl=%.4f target=%.4f roi=%.2f%% '
            'reason=%s timestamp=%.0f',
            self.account_id, basket.symbol, m.get('net_pnl', 0.0),
            m.get('tp_target', 0.0), m.get('roi', 0.0) * 100, reason,
            activation_time, extra=self._log_extra,
        )

    def _release_tp_lock(self, basket: Basket) -> None:
        """Clear the persisted TP lock (only after a confirmed flat closure)."""
        key = self._tp_lock_key(basket.id)
        try:
            self.database.set_state(key, '')
            self.database.set_state(f'{key}_time', '')
        except Exception as e:
            logger.debug('Failed to clear TP lock for %s: %s', basket.id[:8], e)

    def _execute_tp_locked_close(
        self, basket: Basket, reason: str, price: Optional[float]
    ) -> bool:
        """Attempt the committed close; release the lock only on confirmed closure.

        Returns True if the position is now flat (position size 0, exchange
        confirmed) and the lock has been released + TP_LOCK_EXECUTED logged.
        Returns False if the close did not complete (exchange reject / network /
        partial) — the lock STAYS persisted so the next cycle retries.
        ``close_basket`` itself already retries the close order and continues on
        partial fills; this layer adds the persistent, across-restart guarantee.
        """
        trade_logger.info(
            'TP_CLOSE_SENT | account=%s symbol=%s reason=%s timestamp=%.0f',
            self.account_id, basket.symbol, reason, time.time(), extra=self._log_extra,
        )
        trade = self.close_basket(basket, reason)
        if basket.status != 'closed':
            logger.warning(
                'TP_LOCK_RETRY | account=%s symbol=%s reason=%s — close not '
                'confirmed, lock held for next cycle.',
                self.account_id, basket.symbol, reason, extra=self._log_extra,
            )
            return False

        final_pnl = trade.pnl if trade else 0.0
        final_roi = (
            (final_pnl / trade.margin) if (trade and trade.margin > 0) else 0.0
        )
        self._release_tp_lock(basket)
        trade_logger.info(
            'TP_CLOSE_CONFIRMED | account=%s symbol=%s pnl=%.4f target=%s timestamp=%.0f',
            self.account_id, basket.symbol, final_pnl, reason, time.time(),
            extra=self._log_extra,
        )
        trade_logger.info(
            'TP_LOCK_EXECUTED | account=%s symbol=%s final_pnl=%.4f final_roi=%.2f%% '
            'execution_time=%.0f close_reason=%s',
            self.account_id, basket.symbol, final_pnl, final_roi * 100,
            time.time(), reason, extra=self._log_extra,
        )
        return True

    def _close_basket_sl(self, basket: Basket, m: dict) -> None:
        """Close a position that hit its hard stop-loss (reason 'sl').

        Logs SL_HIT with the full breakdown. Closing the position finalizes it.
        """
        trade_logger.info(
            'SL_HIT | account=%s tier=%s symbol=%s gross_pnl=%.4f net_pnl=%.4f '
            'est_fees=%.4f roi=%.2f%% close_reason=sl',
            self.account_id, basket.volatility, basket.symbol,
            m.get('gross_pnl', 0.0), m.get('net_pnl', 0.0), m.get('fee', 0.0),
            m.get('roi', 0.0) * 100, extra=self._log_extra,
        )
        self.close_basket(basket, 'sl')

    # ───────────────────────────────────────────
    # Close operations
    # ───────────────────────────────────────────

    def close_basket(self, basket: Basket, reason: str) -> Optional[TradeRecord]:
        """Close a position (its single layer, idempotent, reduce-only)."""
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

            # ── Close the FULL quantity, continuing on partial fills ──
            # Each reduce-only close reports its actual filled qty; if it only
            # partially fills we keep submitting the REMAINING quantity (never
            # assume a full close). The close is only considered done when the
            # remaining quantity reaches zero (or the exchange reports already
            # flat). A close that never completes returns None so the caller —
            # e.g. an active TP lock — retries on the next cycle without ever
            # releasing the lock.
            remaining_qty = total_qty
            closed_ok = False
            for attempt in range(3):
                try:
                    order = self.exchange.close_position(
                        basket.symbol, basket.side, remaining_qty
                    )
                    filled = self._closed_fill_qty(order, remaining_qty)
                    remaining_qty = max(0.0, remaining_qty - filled)
                    if remaining_qty <= total_qty * 1e-6:
                        closed_ok = True
                        break
                    logger.warning(
                        'PARTIAL_CLOSE | account=%s symbol=%s remaining=%.8f — '
                        'continuing to close the remainder.',
                        self.account_id, basket.symbol, remaining_qty,
                        extra=self._log_extra,
                    )
                    if attempt == 2:
                        logger.critical(
                            'FAILED to fully close position %s after 3 attempts '
                            '(remaining=%.8f).', basket.id[:8], remaining_qty,
                            extra=self._log_extra,
                        )
                        return None
                except Exception as e:
                    if self._is_benign_close_error(e):
                        logger.warning(
                            'Close for %s reports already flat (%s) — finalizing %s.',
                            basket.symbol, e, basket.id[:8], extra=self._log_extra,
                        )
                        closed_ok = True
                        break
                    if attempt == 2:
                        logger.critical(
                            'FAILED to close position %s after 3 attempts: %s',
                            basket.id[:8], e, extra=self._log_extra,
                        )
                        return None
                    time.sleep(0.25)

            if not closed_ok:
                return None

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
                'CLOSE | account=%s tier=%s symbol=%s direction=%s entry=%.6f '
                'exit=%.6f pnl=%s%.4f USDT daily_realized=%.4f cooldown=%dm | reason: %s',
                self.account_id, basket.volatility, basket.symbol, basket.side.upper(),
                trade.entry_price, current_price, sign, realized_pnl,
                daily_realized, self.settings.symbol_cooldown_seconds // 60, reason,
                extra=self._log_extra,
            )
            return trade

        except Exception as e:
            logger.error(
                'Error closing position %s: %s', basket.id[:8], e, extra=self._log_extra,
            )
            return None
        finally:
            with self._closing_lock:
                self._closing.discard(basket.id)

    def close_all_baskets(self, baskets: List[Basket], reason: str) -> List[TradeRecord]:
        """Close every active position (e.g. daily loss limit, force-close)."""
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
        """Finalize DB positions that no longer have a live exchange position."""
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
                'RECONCILE | position %s (%s %s) has no live position — running full '
                'closure workflow.', basket.id[:8], basket.side.upper(), basket.symbol,
                extra=self._log_extra,
            )
            self._finalize_reconciled_basket(basket)

        return still_active

    def _finalize_reconciled_basket(self, basket: Basket) -> Optional[TradeRecord]:
        """Full closure for a position whose exchange position has vanished.

        The position no longer exists on the exchange (closed externally, by a
        prior fill whose trade write was lost, or by liquidation), so there is no
        order to place. This still runs the COMPLETE closure workflow so state is
        never left half-finished:
          1. resolve the exit reason — the committed TP-lock reason if one is
             held, else 'reconciled';
          2. persist a trade record (exit reason, close timestamp, final PnL,
             final ROI) priced at the best available mark; and
          3. release the (possibly orphaned) TP lock + start the cooldown.
        """
        reason = self._tp_lock_reason(basket) or 'reconciled'
        total_qty = basket.total_quantity
        total_margin = basket.total_margin
        layers_used = basket.layer_count
        entry_price = basket.avg_entry_price

        price = self._fetch_price_with_retry(basket.symbol)
        if not price or price <= 0:
            price = entry_price  # no mark available → record a flat (0 PnL) close
        gross = basket.unrealized_pnl(price) if price > 0 else 0.0
        fee = (
            total_qty * price * self.settings.taker_fee_pct * 2
            if (total_qty > 0 and price > 0) else 0.0
        )
        realized_pnl = gross - fee
        roi = (realized_pnl / total_margin) if total_margin > 0 else 0.0

        trade = TradeRecord(
            basket_id=basket.id, symbol=basket.symbol, side=basket.side,
            entry_price=entry_price, exit_price=price, quantity=total_qty,
            margin=total_margin, leverage=basket.leverage, pnl=realized_pnl,
            fee=fee, layers_used=layers_used, entry_time=basket.created_at,
            exit_time=time.time(), exit_reason=reason, account_id=self.account_id,
        )

        basket.close_all()
        self.database.close_basket(basket.id)
        try:
            self.database.save_trade(trade)
        except Exception as e:
            logger.error(
                'RECONCILE trade persist failed for %s: %s',
                basket.id[:8], e, extra=self._log_extra,
            )
        self._finalize_closed_state(basket.symbol, basket.id)
        self._release_tp_lock(basket)   # never leave an orphaned lock behind

        trade_logger.info(
            'RECONCILE_CLOSE | account=%s tier=%s symbol=%s direction=%s '
            'exit=%.6f pnl=%.4f roi=%.2f%% exit_reason=%s',
            self.account_id, basket.volatility, basket.symbol, basket.side.upper(),
            price, realized_pnl, roi * 100, reason, extra=self._log_extra,
        )
        return trade

    # ───────────────────────────────────────────
    # Cooldown + helpers
    # ───────────────────────────────────────────

    def _fetch_price_with_retry(self, symbol: str, attempts: int = 3) -> Optional[float]:
        """Fetch the last price, RETRYING transient ticker failures.

        A single ticker hiccup must never silently defer a position that may be
        due to close (which previously let a hit target reverse before the next
        cycle). Returns the price, or None only after all attempts fail.
        """
        for i in range(attempts):
            try:
                price = float(self.exchange.fetch_ticker(symbol)['last'])
                if price > 0:
                    return price
            except Exception as e:
                logger.debug(
                    'Ticker fetch attempt %d/%d failed for %s: %s',
                    i + 1, attempts, symbol, e,
                )
            if i < attempts - 1:
                time.sleep(0.2)
        return None

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
    def _closed_fill_qty(order, requested_qty: float) -> float:
        """Actual quantity a close order reduced — used for partial-close retries.

        Reads ccxt ``filled``; when the close response does not report a filled
        quantity (some venues/fakes omit it on a reduce-only close), assume the
        full requested quantity closed so a fully-filled close never loops.
        """
        if not isinstance(order, dict):
            return requested_qty
        raw = order.get('filled')
        if raw is None:
            return requested_qty
        try:
            return float(raw)
        except (TypeError, ValueError):
            return requested_qty

    @staticmethod
    def _resolve_fill(order: dict, requested_qty: float, fallback_price: float, leverage: int):
        """Extract the ACTUAL fill from an order — never assume a full fill.

        Reads the truly-filled quantity (ccxt ``filled``, falling back to
        ``amount``) and the average fill price, then derives the ACTUAL margin
        consumed (filled × price / leverage). The take-profit/stop-loss are
        computed from these actual values, so a partial fill is handled correctly.

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
