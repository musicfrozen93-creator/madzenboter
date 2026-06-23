"""Zentry Futures Core — Centralized Bot Control Service.

Thread-safe singleton that governs the bot's operational state:
  - BOT_ENABLED: whether the scanner runs and new trades can open.
  - MANAGE_EXISTING_POSITIONS: whether TP/SL runs on open positions.
  - FORCE_CLOSE_ALL: one-shot flag to safely close every open position.
  - EMERGENCY_STOP: immediately halts scanner, signals, and new positions;
    cancels pending orders; but leaves TP/SL/risk management fully active.
    Positions are NOT force-closed.

Environment variables set the initial state; the admin API can override
at runtime. State resets to env-var defaults on restart (intentional —
no accidental "stuck disabled" after a deploy).
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.database import Database
    from execution.executor import SignalExecutor

logger = logging.getLogger('zentry')
control_logger = logging.getLogger('zentry.control')


@dataclass
class ControlSnapshot:
    """Immutable point-in-time snapshot of bot control state."""
    bot_enabled: bool
    manage_existing_positions: bool
    force_close_all: bool
    emergency_stop: bool
    scanner_running: bool
    last_action: str
    last_action_at: Optional[float]


class BotControl:
    """Centralized, thread-safe bot control service.

    All state reads/writes go through this class so every component sees
    a consistent view. The ``[CONTROL]`` log prefix makes control-plane
    events trivially greppable in production logs.

    Guard methods (call these at every decision point):
      can_open_trades()        — True when new positions may open.
      can_manage_positions()   — True when TP/SL position management may run.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        self._bot_enabled: bool = _env_bool('BOT_ENABLED', True)
        self._manage_existing: bool = _env_bool('MANAGE_EXISTING_POSITIONS', True)
        self._force_close_all: bool = _env_bool('FORCE_CLOSE_ALL', False)
        self._emergency_stop: bool = _env_bool('EMERGENCY_STOP', False)

        self._scanner_running: bool = False
        self._last_action: str = 'initialized'
        self._last_action_at: float = time.time()

        self._force_close_in_progress: bool = False

        control_logger.info(
            '[CONTROL] Initialized | bot_enabled=%s manage_existing=%s '
            'force_close_all=%s emergency_stop=%s',
            self._bot_enabled, self._manage_existing,
            self._force_close_all, self._emergency_stop,
        )

    # ───────────────────────────────────────────
    # Read-only queries (lock-free for hot path)
    # ───────────────────────────────────────────

    @property
    def bot_enabled(self) -> bool:
        return self._bot_enabled

    @property
    def manage_existing_positions(self) -> bool:
        return self._manage_existing

    @property
    def force_close_all(self) -> bool:
        return self._force_close_all

    @property
    def emergency_stop(self) -> bool:
        return self._emergency_stop

    @property
    def scanner_running(self) -> bool:
        return self._scanner_running

    def can_open_trades(self) -> bool:
        """True when new positions/baskets are permitted.

        Blocked by: bot_enabled=False, force_close_all, or emergency_stop.
        """
        return (
            self._bot_enabled
            and not self._force_close_all
            and not self._emergency_stop
        )

    def can_manage_positions(self) -> bool:
        """True when existing-position management (TP/SL) may run."""
        return self._manage_existing

    def snapshot(self) -> ControlSnapshot:
        with self._lock:
            return ControlSnapshot(
                bot_enabled=self._bot_enabled,
                manage_existing_positions=self._manage_existing,
                force_close_all=self._force_close_all,
                emergency_stop=self._emergency_stop,
                scanner_running=self._scanner_running,
                last_action=self._last_action,
                last_action_at=self._last_action_at,
            )

    # ───────────────────────────────────────────
    # Mutators (all log with [CONTROL] prefix)
    # ───────────────────────────────────────────

    def start_bot(self) -> None:
        with self._lock:
            if self._bot_enabled and not self._emergency_stop:
                control_logger.info('[CONTROL] Bot already running — no-op')
                return
            self._bot_enabled = True
            self._emergency_stop = False
            self._record_action('Bot Started')
        control_logger.info('[CONTROL] Bot Started')

    def stop_bot(self) -> None:
        with self._lock:
            if not self._bot_enabled:
                control_logger.info('[CONTROL] Bot already stopped — no-op')
                return
            self._bot_enabled = False
            self._record_action('Bot Stopped')
        control_logger.info('[CONTROL] Bot Stopped')
        control_logger.info('[CONTROL] New Trades Disabled')

    def enable_position_management(self) -> None:
        with self._lock:
            self._manage_existing = True
            self._record_action('Position Management Enabled')
        control_logger.info('[CONTROL] Managing Existing Positions')

    def disable_position_management(self) -> None:
        with self._lock:
            self._manage_existing = False
            self._record_action('Position Management Disabled')
        control_logger.info('[CONTROL] Position Management Disabled (monitoring only)')

    def set_scanner_running(self, running: bool) -> None:
        self._scanner_running = running

    # ───────────────────────────────────────────
    # Emergency Stop
    # ───────────────────────────────────────────

    def set_emergency_stop(self) -> None:
        """Activate EMERGENCY_STOP mode.

        Immediately halts:
          • Scanner
          • Signal execution
          • New position opening

        Deliberately does NOT touch TP/SL/risk management —
        existing positions remain fully protected.
        Positions are NOT force-closed (use request_force_close_all for that).
        Pending order cancellation must be handled by the caller (executor).
        """
        with self._lock:
            if self._emergency_stop:
                control_logger.info('[CONTROL] Emergency Stop already active — no-op')
                return
            self._emergency_stop = True
            self._bot_enabled = False   # Belt-and-suspenders: also disable bot
            self._record_action('Emergency Stop Activated')

        control_logger.info('[CONTROL] EMERGENCY STOP ACTIVATED')
        control_logger.info('[CONTROL] New Trades Disabled')
        control_logger.info('[CONTROL] Scanner Disabled')
        control_logger.info('[CONTROL] TP/SL remain ACTIVE — positions protected')

    def clear_emergency_stop(self) -> None:
        """Deactivate EMERGENCY_STOP mode and re-enable the bot."""
        with self._lock:
            if not self._emergency_stop:
                control_logger.info('[CONTROL] Emergency Stop not active — no-op')
                return
            self._emergency_stop = False
            self._bot_enabled = True
            self._record_action('Emergency Stop Cleared')

        control_logger.info('[CONTROL] Emergency Stop Cleared — Bot Resumed')

    # ───────────────────────────────────────────
    # Force Close All
    # ───────────────────────────────────────────

    def request_force_close_all(
        self,
        executor: 'SignalExecutor',
        database: 'Database',
    ) -> dict:
        """Safely close ALL open positions across ALL accounts.

        Sequence:
          1. Disable new trades (bot_enabled = False).
          2. Iterate every managed account; close baskets one by one.
          3. Cancel any pending orders (handled inside close_basket).
          4. Log every action.
          5. Generate a summary report.
          6. Reset force_close_all flag.

        Returns:
            Summary dict with counts and per-account details.
        """
        with self._lock:
            if self._force_close_in_progress:
                return {'error': 'Force close already in progress'}
            self._force_close_in_progress = True

        control_logger.info('[CONTROL] Close All Requested')

        # Step 1: disable new trades immediately
        with self._lock:
            self._bot_enabled = False
            self._force_close_all = True
            self._record_action('Close All Requested')

        control_logger.info('[CONTROL] New Trades Disabled (pre-close)')

        summary = {
            'accounts_processed': 0,
            'baskets_closed': 0,
            'baskets_failed': 0,
            'details': [],
        }

        try:
            managed_accounts = database.get_managed_accounts()
            for account in managed_accounts:
                acct_detail = {
                    'account_id': account.id,
                    'label': account.label,
                    'baskets_closed': 0,
                    'baskets_failed': 0,
                }

                try:
                    components = executor._build_account_components(account)
                    if not components:
                        control_logger.warning(
                            '[CONTROL] Could not build components for account %s — skipping',
                            account.id,
                        )
                        continue

                    exchange_client, acct_settings, position_manager, risk_manager = components
                    baskets = database.load_active_baskets(account_id=account.id)

                    if not baskets:
                        summary['accounts_processed'] += 1
                        summary['details'].append(acct_detail)
                        continue

                    balance_dict = exchange_client.fetch_balance()
                    balance = balance_dict['total']
                    risk_manager.initialize(balance)

                    for basket in baskets:
                        if basket.status != 'active':
                            continue
                        try:
                            trade = position_manager.close_basket(basket, 'force_close_all')
                            if trade:
                                control_logger.info(
                                    '[CONTROL] Position Closed | account=%s symbol=%s '
                                    'side=%s pnl=%.4f',
                                    account.id, basket.symbol,
                                    basket.side, trade.pnl,
                                )
                                acct_detail['baskets_closed'] += 1
                                summary['baskets_closed'] += 1
                            else:
                                acct_detail['baskets_closed'] += 1
                                summary['baskets_closed'] += 1
                        except Exception as e:
                            control_logger.error(
                                '[CONTROL] Failed to close basket %s for account %s: %s',
                                basket.id[:8], account.id, e,
                            )
                            acct_detail['baskets_failed'] += 1
                            summary['baskets_failed'] += 1

                except Exception as e:
                    control_logger.error(
                        '[CONTROL] Error processing account %s during force close: %s',
                        account.id, e,
                    )

                summary['accounts_processed'] += 1
                summary['details'].append(acct_detail)

        finally:
            with self._lock:
                self._force_close_all = False
                self._force_close_in_progress = False
                self._record_action('Close All Completed')

        control_logger.info(
            '[CONTROL] Close All Completed | accounts=%d closed=%d failed=%d',
            summary['accounts_processed'],
            summary['baskets_closed'],
            summary['baskets_failed'],
        )

        return summary

    # ───────────────────────────────────────────
    # Internal
    # ───────────────────────────────────────────

    def _record_action(self, action: str) -> None:
        """Update last-action tracking (caller must hold _lock)."""
        self._last_action = action
        self._last_action_at = time.time()


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable (true/false/1/0)."""
    val = os.environ.get(name, '').strip().lower()
    if val in ('true', '1', 'yes'):
        return True
    if val in ('false', '0', 'no'):
        return False
    return default
