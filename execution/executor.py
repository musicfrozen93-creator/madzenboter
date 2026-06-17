"""ZenGrid — Multi-Account Signal Executor.

Fans out trading signals to all active accounts for independent execution.
Each account gets its own ExchangeClient, RiskManager, and PositionManager
instances. Failure in one account does not affect others.

Designed to scale to 500+ concurrent accounts using ThreadPoolExecutor
with batch processing.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Tuple

from accounts.encryption import EncryptionService
from accounts.manager import AccountManager
from config.settings import Settings, VolatilityLevel
from control.bot_control import BotControl
from core.database import Database
from core.dto import Basket, Signal, TradeRecord
from core.models import AccountModel
from exchange.client import ExchangeClient
from grid.position_manager import PositionManager
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from risk.stop_loss import StopLossManager
from signals.signal_engine import SignalEngine

logger = logging.getLogger('execution')
control_logger = logging.getLogger('zentry.control')


class AccountDatabaseWrapper:
    """Wraps Database to isolate queries and state to a specific account."""

    def __init__(self, db: Database, account_id: int) -> None:
        self._db = db
        self._account_id = account_id

    def __getattr__(self, name: str):
        # Delegate any other attribute/method access to the underlying database
        return getattr(self._db, name)

    def load_active_baskets(self, account_id: Optional[int] = None) -> List[Basket]:
        # Always force the current account's baskets
        return self._db.load_active_baskets(account_id=self._account_id)

    def save_basket(self, basket: Basket) -> None:
        basket.account_id = self._account_id
        self._db.save_basket(basket)

    def save_trade(self, trade: TradeRecord) -> None:
        trade.account_id = self._account_id
        self._db.save_trade(trade)

    def get_state(self, key: str) -> Optional[str]:
        # Prefix the state key for account isolation
        return self._db.get_state(f'account_{self._account_id}_{key}')

    def set_state(self, key: str, value: str) -> None:
        # Prefix the state key for account isolation
        self._db.set_state(f'account_{self._account_id}_{key}', value)

    def save_daily_stats(self, stats: dict, account_id: Optional[int] = None) -> None:
        # Force the account_id
        self._db.save_daily_stats(stats, account_id=self._account_id)


@dataclass
class ExecutionResult:
    """Represents the execution outcome of a signal for a single account."""
    account_id: int
    account_label: str
    success: bool
    basket_id: Optional[str] = None
    error: Optional[str] = None


class SignalExecutor:
    """Orchestrates signal fan-out to multiple accounts concurrently."""

    def __init__(
        self,
        db: Database,
        account_manager: AccountManager,
        encryption: EncryptionService,
        master_settings: Settings,
        max_workers: int = 50,
        batch_size: int = 50,
        bot_control: Optional[BotControl] = None,
    ) -> None:
        """Initialise the signal executor.

        Args:
            db: Database instance.
            account_manager: Account manager for fetching active accounts.
            encryption: Encryption service for decrypting API keys.
            master_settings: Global master settings.
            max_workers: ThreadPool max workers.
            batch_size: Account batch processing size.
            bot_control: Centralized BotControl singleton for runtime guards.
        """
        self.db = db
        self.account_manager = account_manager
        self.encryption = encryption
        self.master_settings = master_settings
        self.max_workers = max_workers
        self.batch_size = batch_size
        self.bot_control = bot_control

        # ── Per-account component cache ──
        # Building components calls exchange_client.initialize() → load_markets()
        # (a full network round-trip). Without caching this ran once per account
        # in manage_all_accounts AND once per account PER COIN during the signal
        # fan-out — the dominant source of exit-close latency. We cache the built
        # tuple per account and reuse it across loops; it is rebuilt only when the
        # account's credentials or risk settings change. Balance/risk state are
        # always refreshed at use-time, so cached components never go stale.
        self._component_cache: dict = {}
        self._cache_lock = threading.Lock()

    def execute_signal(self, signal: Signal) -> List[ExecutionResult]:
        """Save signal and execute it concurrently on all active accounts.

        CONTROL GATE: blocked when bot_control.can_open_trades() is False
        (covers BOT_ENABLED=false, EMERGENCY_STOP, and FORCE_CLOSE_ALL).

        Args:
            signal: Signal generated by the master engine.

        Returns:
            List of ExecutionResults for all accounts, or [] if blocked.
        """
        # ── BOT_CONTROL gate ──
        if self.bot_control and not self.bot_control.can_open_trades():
            control_logger.info(
                '[CONTROL] Signal BLOCKED %s %s — bot_enabled=%s emergency_stop=%s '
                'force_close_all=%s',
                signal.side.upper(), signal.symbol,
                self.bot_control.bot_enabled,
                self.bot_control.emergency_stop,
                self.bot_control.force_close_all,
            )
            return []

        # Save signal to DB to get its ID
        signal_id = self.db.save_signal(signal)
        logger.info('Saved signal %s %s with ID %s', signal.side.upper(), signal.symbol, signal_id)

        # ── Eligibility gate (subscription enforcement) ──
        # The database is the source of truth. Each account is evaluated
        # independently; ineligible ones (suspended user, expired/missing
        # subscription, disabled account) are skipped and logged, never traded.
        eligibility = self.db.get_account_eligibility()
        active_accounts = [acct for (acct, ok, _reason) in eligibility if ok]

        for acct, ok, reason in eligibility:
            if ok:
                continue
            logger.info(
                'Skipping account %s (%s) for %s %s: %s',
                acct.id, acct.label, signal.side.upper(), signal.symbol, reason,
                extra={'account_id': acct.id},
            )
            try:
                self.db.save_execution_log(
                    account_id=acct.id, action='open', symbol=signal.symbol,
                    status='skipped', signal_id=signal_id, side=signal.side,
                    error_message=f'ineligible: {reason}',
                )
            except Exception as db_exc:
                logger.debug('Failed to log skip for account %s: %s', acct.id, db_exc)

        if not active_accounts:
            logger.warning(
                'No eligible accounts (active user + active subscription + enabled '
                'account) for signal %s %s — not trading.',
                signal.side.upper(), signal.symbol,
            )
            return []

        results: List[ExecutionResult] = []
        # Process accounts in batches to manage resources and rate limits
        for i in range(0, len(active_accounts), self.batch_size):
            batch = active_accounts[i:i + self.batch_size]
            logger.info('Processing signal execution batch of %d accounts', len(batch))

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(
                        self._execute_for_account, account, signal, signal_id
                    ): account
                    for account in batch
                }

                for future in as_completed(futures):
                    try:
                        res = future.result()
                        results.append(res)
                    except Exception as exc:
                        acct = futures[future]
                        logger.error(
                            'Unhanded exception during execution for account %s (%s): %s',
                            acct.id, acct.label, exc,
                            extra={'account_id': acct.id}
                        )
                        results.append(ExecutionResult(
                            account_id=acct.id,
                            account_label=acct.label,
                            success=False,
                            error=str(exc)
                        ))

        # Log execution summary
        success_count = sum(1 for r in results if r.success)
        logger.info(
            'Signal execution complete. Success: %d/%d accounts',
            success_count, len(results)
        )
        return results

    def _execute_for_account(
        self, account: AccountModel, signal: Signal, signal_id: int
    ) -> ExecutionResult:
        """Execute a signal on a single account.

        Args:
            account: AccountModel ORM instance.
            signal: Signal DTO.
            signal_id: Database ID of the signal.

        Returns:
            ExecutionResult.
        """
        try:
            # Build account-specific components (auto-decrypts keys internally)
            components = self._build_account_components(account)
            if not components:
                raise Exception('Failed to initialise trading components')

            exchange_client, acct_settings, position_manager, risk_manager = components

            # Fetch account balance from exchange
            balance_dict = exchange_client.fetch_balance()
            balance = balance_dict['total']

            # Initialize risk state for this account
            risk_manager.initialize(balance)

            # Attempt to open the position
            basket = position_manager.open_position(signal, balance)

            if basket:
                # NOTE: open_position() already persisted the basket and its
                # layers through the account-isolated DB wrapper (which stamps
                # account_id). Do NOT save again here — Database.save_basket
                # INSERTs layers, so a second save duplicates every layer and
                # doubles the tracked quantity/margin/PnL.
                basket.account_id = account.id

                fill_price = basket.layers[-1].entry_price if basket.layers else signal.current_price
                qty = basket.layers[-1].quantity if basket.layers else 0.0

                self.db.save_execution_log(
                    account_id=account.id,
                    action='open',
                    symbol=signal.symbol,
                    status='success',
                    signal_id=signal_id,
                    side=signal.side,
                    quantity=qty,
                    price=fill_price
                )

                logger.info(
                    'Opened position for account %s (%s) on %s | basket=%s',
                    account.id, account.label, signal.symbol, basket.id[:8],
                    extra={'account_id': account.id}
                )

                return ExecutionResult(
                    account_id=account.id,
                    account_label=account.label,
                    success=True,
                    basket_id=basket.id
                )
            else:
                self.db.save_execution_log(
                    account_id=account.id,
                    action='open',
                    symbol=signal.symbol,
                    status='skipped',
                    signal_id=signal_id,
                    side=signal.side,
                    error_message='Skipped by risk/position limits'
                )

                logger.info(
                    'Skipped signal for account %s (%s) on %s',
                    account.id, account.label, signal.symbol,
                    extra={'account_id': account.id}
                )

                return ExecutionResult(
                    account_id=account.id,
                    account_label=account.label,
                    success=True,
                    error='Signal skipped'
                )

        except Exception as e:
            logger.error(
                'Execution error for account %s (%s): %s',
                account.id, account.label, e,
                extra={'account_id': account.id}
            )
            try:
                self.db.save_execution_log(
                    account_id=account.id,
                    action='open',
                    symbol=signal.symbol,
                    status='failed',
                    signal_id=signal_id,
                    side=signal.side,
                    error_message=str(e)
                )
            except Exception as db_exc:
                logger.error('Failed to save error execution log to DB: %s', db_exc)

            return ExecutionResult(
                account_id=account.id,
                account_label=account.label,
                success=False,
                error=str(e)
            )

    def manage_all_accounts(self) -> None:
        """Manage baskets/positions across all managed accounts CONCURRENTLY.

        Exit handling (TP/SL/profit-protection/emergency) runs here. Accounts are
        processed in parallel on a thread pool so that when several accounts need
        to close at once, later accounts are NOT delayed behind earlier ones.

        "Managed" means: enabled (is_active) OR currently holding an open basket.
        This ensures that when a subscription expires and the web sets is_active=False,
        any open positions are still monitored for TP/SL/recovery until they close
        naturally. New entries are blocked separately by the eligibility check in
        execute_signal(), so there is no risk of opening positions for expired accounts.
        """
        managed_accounts = self.db.get_managed_accounts()
        if not managed_accounts:
            return

        # Forget components for accounts that are no longer managed.
        self._prune_component_cache({a.id for a in managed_accounts})

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [
                executor.submit(self._manage_account_baskets, account)
                for account in managed_accounts
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    logger.error('Error in background account basket management: %s', exc)

    def _manage_account_baskets(self, account: AccountModel) -> None:
        """Sync and manage active baskets for a single account.

        Args:
            account: AccountModel ORM instance.
        """
        try:
            # Build account components
            components = self._build_account_components(account)
            if not components:
                return

            exchange_client, acct_settings, position_manager, risk_manager = components

            # Load active baskets for this account
            baskets = self.db.load_active_baskets(account_id=account.id)
            if not baskets:
                return

            # Fetch live balance
            balance_dict = exchange_client.fetch_balance()
            balance = balance_dict['total']

            # Initialize risk state
            risk_manager.initialize(balance)

            # Reconcile first: drop any DB baskets whose exchange position is gone
            # so stale baskets never block re-entry or distort exposure.
            baskets = position_manager.reconcile_baskets(baskets)
            if not baskets:
                return

            # Let position manager check/handle take-profit, stop-loss, and recovery triggers
            position_manager.manage_baskets(baskets, balance)

        except Exception as e:
            logger.error(
                'Basket management failed for account %s (%s): %s',
                account.id, account.label, e,
                extra={'account_id': account.id}
            )

    def close_account_baskets(self, account_id: int, reason: str) -> None:
        """Close all active positions/baskets for a specific account.

        Args:
            account_id: Account ID.
            reason: Reason for closure (e.g. 'manual', 'api_disabled', 'risk').
        """
        try:
            account = self.account_manager.get_account(account_id)
            if not account:
                logger.error('Account not found: %s', account_id)
                return

            components = self._build_account_components(account)
            if not components:
                return

            exchange_client, acct_settings, position_manager, risk_manager = components

            # Load active baskets
            baskets = self.db.load_active_baskets(account_id=account_id)
            if not baskets:
                return

            # Fetch balance
            balance_dict = exchange_client.fetch_balance()
            balance = balance_dict['total']

            # Initialize risk state
            risk_manager.initialize(balance)

            for basket in baskets:
                position_manager.close_basket(basket, reason)

            logger.info(
                'Closed all active baskets for account %s (%s). Reason: %s',
                account_id, account.label, reason,
                extra={'account_id': account_id}
            )

        except Exception as e:
            logger.error(
                'Failed to close baskets for account %s: %s',
                account_id, e,
                extra={'account_id': account_id}
            )

    @staticmethod
    def _component_fingerprint(account: AccountModel) -> tuple:
        """Fingerprint of every account field that affects component construction.

        Any change (key rotation, testnet toggle, risk/leverage/TP/SL overrides)
        changes the fingerprint and forces a rebuild; otherwise the cached
        components (and their already-loaded markets) are reused.
        """
        return (
            account.encrypted_api_key,
            account.encrypted_api_secret,
            account.use_testnet,
            account.risk_pct,
            account.leverage_override,
            str(account.tp_settings),
            str(account.sl_settings),
        )

    def _prune_component_cache(self, active_ids: set) -> None:
        """Drop cached components for accounts that are no longer active."""
        with self._cache_lock:
            for aid in [a for a in self._component_cache if a not in active_ids]:
                self._component_cache.pop(aid, None)

    def _build_account_components(
        self, account: AccountModel
    ) -> Optional[Tuple[ExchangeClient, Settings, PositionManager, RiskManager]]:
        """Return cached components for an account, rebuilding only on change.

        Reuses the cached exchange client (with markets already loaded) and
        managers when the account's credential/risk fingerprint is unchanged,
        eliminating the repeated load_markets() that previously ran on every
        loop and every signal fan-out. Construction happens OUTSIDE the cache
        lock so concurrent management of different accounts is never serialized.

        Args:
            account: AccountModel instance.

        Returns:
            Tuple of (exchange_client, settings, position_manager, risk_manager) or None.
        """
        fingerprint = self._component_fingerprint(account)
        with self._cache_lock:
            cached = self._component_cache.get(account.id)
        if cached and cached[0] == fingerprint:
            return cached[1]

        components = self._construct_account_components(account)
        if components is None:
            return None

        with self._cache_lock:
            self._component_cache[account.id] = (fingerprint, components)
        return components

    def _construct_account_components(
        self, account: AccountModel
    ) -> Optional[Tuple[ExchangeClient, Settings, PositionManager, RiskManager]]:
        """Build settings, exchange client, and managers for an account (uncached).

        Args:
            account: AccountModel instance.

        Returns:
            Tuple of (exchange_client, settings, position_manager, risk_manager) or None.
        """
        try:
            # Decrypt API keys
            api_key, api_secret = self.account_manager.decrypt_account_keys(account)

            # Build overrides dictionary
            overrides = {
                'risk_pct': account.risk_pct,
                'max_positions': account.max_positions,
                'leverage_override': account.leverage_override,
                'tp_settings': account.tp_settings,
                'sl_settings': account.sl_settings,
            }

            # Create specific settings bound to this account
            acct_settings = Settings.create_account_settings(self.master_settings, overrides)

            # Construct ccxt exchange client for this account
            exchange_client = ExchangeClient.for_account(
                acct_settings, api_key, api_secret
            )
            exchange_client.initialize()

            # Create account-isolated database wrapper
            acct_db = AccountDatabaseWrapper(self.db, account.id)

            # Create risk manager
            risk_manager = RiskManager(acct_settings, acct_db)

            # Instantiate standard managers
            sizer = PositionSizer(acct_settings)
            sl_manager = StopLossManager(acct_settings)
            recovery = RecoverySystem(acct_settings)
            tp_manager = TakeProfitManager(acct_settings)
            signal_engine = SignalEngine(exchange_client, acct_settings)

            position_manager = PositionManager(
                exchange_client=exchange_client,
                settings=acct_settings,
                database=acct_db,
                risk_manager=risk_manager,
                position_sizer=sizer,
                recovery_system=recovery,
                tp_manager=tp_manager,
                sl_manager=sl_manager,
                signal_engine=signal_engine,
                bot_control=self.bot_control,
            )

            return exchange_client, acct_settings, position_manager, risk_manager

        except Exception as e:
            logger.error(
                'Failed to construct trading components for account %s (%s): %s',
                account.id, account.label, e,
                extra={'account_id': account.id}
            )
            return None

    def cancel_all_pending_orders(self, database: Database) -> dict:
        """Cancel all pending (open) orders across ALL managed accounts.

        Called by the emergency-stop API handler immediately after
        bot_control.set_emergency_stop() to prevent any pending entries
        from filling after the bot is halted.

        Returns:
            Summary dict: accounts_processed, orders_cancelled, orders_failed.
        """
        summary = {
            'accounts_processed': 0,
            'orders_cancelled': 0,
            'orders_failed': 0,
            'details': [],
        }

        managed_accounts = database.get_managed_accounts()
        for account in managed_accounts:
            acct_detail = {
                'account_id': account.id,
                'label': account.label,
                'orders_cancelled': 0,
                'orders_failed': 0,
            }
            try:
                components = self._build_account_components(account)
                if not components:
                    control_logger.warning(
                        '[CONTROL] Cannot cancel orders for account %s — no components',
                        account.id,
                    )
                    summary['details'].append(acct_detail)
                    continue

                exchange_client, _, _, _ = components

                try:
                    # ccxt cancel_all_orders: symbol=None cancels across all pairs.
                    open_orders = exchange_client.client.fetch_open_orders()
                    for order in open_orders:
                        try:
                            exchange_client.client.cancel_order(
                                order['id'], order.get('symbol')
                            )
                            acct_detail['orders_cancelled'] += 1
                            summary['orders_cancelled'] += 1
                            control_logger.info(
                                '[CONTROL] Pending order cancelled | account=%s '
                                'order=%s symbol=%s',
                                account.id, order['id'], order.get('symbol'),
                            )
                        except Exception as oe:
                            control_logger.warning(
                                '[CONTROL] Failed to cancel order %s for account %s: %s',
                                order.get('id'), account.id, oe,
                            )
                            acct_detail['orders_failed'] += 1
                            summary['orders_failed'] += 1
                except Exception as fe:
                    control_logger.error(
                        '[CONTROL] Failed to fetch open orders for account %s: %s',
                        account.id, fe,
                    )

            except Exception as e:
                control_logger.error(
                    '[CONTROL] Error during order cancellation for account %s: %s',
                    account.id, e,
                )

            summary['accounts_processed'] += 1
            summary['details'].append(acct_detail)

        control_logger.info(
            '[CONTROL] Pending order cancellation complete | accounts=%d '
            'cancelled=%d failed=%d',
            summary['accounts_processed'],
            summary['orders_cancelled'],
            summary['orders_failed'],
        )
        return summary
