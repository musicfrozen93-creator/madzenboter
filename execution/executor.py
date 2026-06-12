"""ZenGrid — Multi-Account Signal Executor.

Fans out trading signals to all active accounts for independent execution.
Each account gets its own ExchangeClient, RiskManager, and PositionManager
instances. Failure in one account does not affect others.

Designed to scale to 500+ concurrent accounts using ThreadPoolExecutor
with batch processing.
"""

import copy
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from accounts.encryption import EncryptionService
from accounts.manager import AccountManager
from config.settings import Settings, VolatilityLevel
from core.database import Database
from core.dto import Basket, Signal, TradeRecord
from core.models import AccountModel
from exchange.client import ExchangeClient
from grid.position_manager import PositionManager
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from grid.templates import TemplateRouter
from market.market_state import MarketState
from market.symbol_state import SymbolStateEngine
from portfolio.manager import PortfolioManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from risk.stop_loss import StopLossManager
from signals.signal_engine import SignalEngine

logger = logging.getLogger('execution')

# Per-account component cache TTL (C4 fix). Rebuilding the component stack
# — including the exchange client and its market load — on every loop for
# every account multiplied REST weight linearly with account count and made
# the PositionManager's ATR cache useless. Components are reused across
# loops and invalidated when the account row changes (credential rotation /
# settings update bumps updated_at) or after this TTL.
_COMPONENT_CACHE_TTL_SECONDS = 900.0


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

    def get_trades_since(
        self, timestamp: float, account_id: Optional[int] = None
    ) -> List[TradeRecord]:
        # Always scope trade history to this account (V2 portfolio event
        # budget reads recent realized losses through this method).
        return self._db.get_trades_since(timestamp, account_id=self._account_id)


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
        symbol_state_engine: Optional[SymbolStateEngine] = None,
    ) -> None:
        """Initialise the signal executor.

        Args:
            db: Database instance.
            account_manager: Account manager for fetching active accounts.
            encryption: Encryption service for decrypting API keys.
            master_settings: Global master settings.
            max_workers: ThreadPool max workers.
            batch_size: Account batch processing size.
            symbol_state_engine: Shared V2 SymbolStateEngine (classified once
                from public market data, consulted by every account's premise
                monitor). Optional; None = V1-equivalent behaviour.
        """
        self.db = db
        self.account_manager = account_manager
        self.encryption = encryption
        self.master_settings = master_settings
        self.max_workers = max_workers
        self.batch_size = batch_size
        self.symbol_state_engine = symbol_state_engine
        # C4: per-account component cache — account_id -> dict with
        # built_at / fingerprint / components.
        self._component_cache: Dict[int, dict] = {}
        self._component_lock = threading.Lock()

    def execute_signal(
        self,
        signal: Signal,
        market_state: Optional[MarketState] = None,
    ) -> List[ExecutionResult]:
        """Save signal and execute it concurrently on all active accounts.

        Args:
            signal: Signal generated by the master engine.
            market_state: Global V2 market state — routed to every account's
                template router and portfolio manager.

        Returns:
            List of ExecutionResults for all accounts.
        """
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
                        self._execute_for_account, account, signal, signal_id,
                        market_state,
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
        self,
        account: AccountModel,
        signal: Signal,
        signal_id: int,
        market_state: Optional[MarketState] = None,
    ) -> ExecutionResult:
        """Execute a signal on a single account.

        Args:
            account: AccountModel ORM instance.
            signal: Signal DTO.
            signal_id: Database ID of the signal.
            market_state: Global V2 market state.

        Returns:
            ExecutionResult.
        """
        # H5: each account thread works on its OWN copy of the signal.
        # Routing legitimately differs per account (demotions, portfolio
        # state) and open_position writes alignment_score onto the signal —
        # mutating one DTO shared across up to 50 executor threads was a
        # data race and cross-account state contamination. All Signal
        # fields are immutable scalars, so a shallow copy fully isolates.
        signal = copy.copy(signal)

        try:
            # Build account-specific components (auto-decrypts keys internally)
            components = self._get_account_components(account)
            if not components:
                raise Exception('Failed to initialise trading components')

            exchange_client, acct_settings, position_manager, risk_manager = components

            # Fetch account balance from exchange
            balance_dict = exchange_client.fetch_balance()
            balance = balance_dict['total']

            # Initialize risk state for this account
            risk_manager.initialize(balance)

            # Attempt to open the position (V2: template routing + portfolio
            # budgets run inside open_position with the shared market state)
            basket = position_manager.open_position(signal, balance, market_state)

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

    def manage_all_accounts(
        self, market_state: Optional[MarketState] = None
    ) -> None:
        """Periodically manage baskets/positions across all active accounts.

        Args:
            market_state: Global V2 market state — used by every account's
                premise monitor and recovery gates.
        """
        active_accounts = self.account_manager.get_active_accounts()
        if not active_accounts:
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [
                executor.submit(self._manage_account_baskets, account, market_state)
                for account in active_accounts
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    logger.error('Error in background account basket management: %s', exc)

    def _manage_account_baskets(
        self,
        account: AccountModel,
        market_state: Optional[MarketState] = None,
    ) -> None:
        """Sync and manage active baskets for a single account.

        Args:
            account: AccountModel ORM instance.
            market_state: Global V2 market state.
        """
        try:
            # Build account components
            components = self._get_account_components(account)
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

            # Let position manager check/handle take-profit, stop-loss, and recovery triggers
            position_manager.manage_baskets(baskets, balance, market_state)

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

            components = self._get_account_components(account)
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

    def _get_account_components(
        self, account: AccountModel
    ) -> Optional[Tuple[ExchangeClient, Settings, PositionManager, RiskManager]]:
        """Cached per-account trading components (C4 fix).

        Returns the cached component stack when the account row is unchanged
        and the cache entry is fresh; otherwise rebuilds. The fingerprint is
        the account's ``updated_at`` — credential rotation, enable/disable,
        and settings overrides all bump it, forcing a rebuild with the new
        values. Failed builds are never cached.

        Args:
            account: AccountModel ORM instance.

        Returns:
            Tuple of (exchange_client, settings, position_manager,
            risk_manager) or None when construction fails.
        """
        fingerprint = self._account_fingerprint(account)
        now = time.time()

        with self._component_lock:
            entry = self._component_cache.get(account.id)
            if (
                entry is not None
                and entry['fingerprint'] == fingerprint
                and now - entry['built_at'] < _COMPONENT_CACHE_TTL_SECONDS
            ):
                return entry['components']

        components = self._build_account_components(account)
        if components is not None:
            with self._component_lock:
                self._component_cache[account.id] = {
                    'built_at': now,
                    'fingerprint': fingerprint,
                    'components': components,
                }
        return components

    @staticmethod
    def _account_fingerprint(account: AccountModel) -> str:
        """Component-cache fingerprint from TRADING-RELEVANT fields only.

        PARTICIPATION-REGRESSION FIX: the original C4 fingerprint used
        ``account.updated_at`` — but the sync service writes cached_balance
        and last_sync_at every 60 seconds, which bumps updated_at via the
        column's onupdate trigger. The cache was therefore invalidated
        every sync cycle, silently reducing the intended 15-minute reuse to
        ~60 seconds and reintroducing per-loop client rebuilds and market
        loads. Credential rotation and settings changes still invalidate:
        the fingerprint covers every field that affects how the account
        trades, and nothing that changes as a side effect of syncing.
        """
        return '|'.join(
            str(getattr(account, field_name, ''))
            for field_name in (
                'encrypted_api_key', 'encrypted_api_secret', 'use_testnet',
                'risk_pct', 'max_positions', 'leverage_override',
                'tp_settings', 'sl_settings', 'is_active',
            )
        )

    def _build_account_components(
        self, account: AccountModel
    ) -> Optional[Tuple[ExchangeClient, Settings, PositionManager, RiskManager]]:
        """Helper to build settings, exchange client, and managers for an account.

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

            # V2 per-account components: template router + portfolio budgets
            # (the symbol state engine is SHARED — classified once from
            # public market data, consulted by every account).
            template_router = TemplateRouter(acct_settings)
            portfolio_manager = PortfolioManager(acct_settings)

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
                template_router=template_router,
                portfolio_manager=portfolio_manager,
                symbol_state_engine=self.symbol_state_engine,
            )

            return exchange_client, acct_settings, position_manager, risk_manager

        except Exception as e:
            logger.error(
                'Failed to construct trading components for account %s (%s): %s',
                account.id, account.label, e,
                extra={'account_id': account.id}
            )
            return None
