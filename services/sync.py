"""ZenGrid — Background Sync Services.

Periodic background tasks for syncing account balances, positions,
trades, and risk metrics across all active accounts.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from accounts.encryption import EncryptionService
from accounts.manager import AccountManager
from config.settings import Settings
from core.database import Database
from core.models import AccountModel, PositionModel, RiskMetricModel
from exchange.client import ExchangeClient

logger = logging.getLogger(__name__)


class SyncService:
    """Background service that periodically syncs exchange data for all accounts.

    Runs in a dedicated daemon thread and performs balance, position,
    trade, and risk-metric synchronisation on a configurable interval.
    Each account is handled independently — a failure on one account
    does not interrupt processing of the remaining accounts.

    Args:
        db: Database repository instance.
        account_manager: AccountManager for retrieving active accounts.
        encryption: EncryptionService for decrypting stored API credentials.
        settings: Global application settings.
        interval_seconds: Seconds to sleep between sync cycles (default 60).
    """

    def __init__(
        self,
        db: Database,
        account_manager: AccountManager,
        encryption: EncryptionService,
        settings: Settings,
        interval_seconds: int = 60,
    ) -> None:
        self._db: Database = db
        self._account_manager: AccountManager = account_manager
        self._encryption: EncryptionService = encryption
        self._settings: Settings = settings
        self._interval_seconds: int = interval_seconds

        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()

    # ───────────────────────────────────────────
    # Lifecycle
    # ───────────────────────────────────────────

    def start(self) -> None:
        """Start the background sync loop in a daemon thread.

        If the service is already running, this method is a no-op.
        """
        if self._running:
            logger.warning('SyncService is already running')
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._sync_loop,
            name='sync-service',
            daemon=True,
        )
        self._thread.start()
        logger.info(
            'SyncService started (interval=%ds)', self._interval_seconds
        )

    def stop(self) -> None:
        """Signal the sync loop to stop and wait for the thread to exit.

        Blocks for up to ``interval_seconds + 5`` to allow the current
        cycle to finish gracefully.
        """
        if not self._running:
            logger.warning('SyncService is not running')
            return

        logger.info('Stopping SyncService...')
        self._running = False
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._interval_seconds + 5)
            if self._thread.is_alive():
                logger.warning(
                    'SyncService thread did not terminate within timeout'
                )
        self._thread = None
        logger.info('SyncService stopped')

    # ───────────────────────────────────────────
    # Main Loop
    # ───────────────────────────────────────────

    def _sync_loop(self) -> None:
        """Main sync loop executed in the background thread.

        Runs balance, position, and risk-metric sync tasks in sequence,
        then sleeps for the configured interval. The loop exits when
        ``stop()`` is called.
        """
        logger.info('Sync loop started')
        while self._running:
            cycle_start = time.monotonic()
            try:
                self.sync_balances()
                self.sync_positions()
                self.sync_trades()
                self.update_risk_metrics()
            except Exception:
                logger.exception('Unhandled error in sync cycle')

            elapsed = time.monotonic() - cycle_start
            logger.info(
                'Sync cycle completed in %.2fs, sleeping %ds',
                elapsed,
                self._interval_seconds,
            )

            # Use the stop event for interruptible sleep
            if self._stop_event.wait(timeout=self._interval_seconds):
                break

        logger.info('Sync loop exited')

    # ───────────────────────────────────────────
    # Balance Sync
    # ───────────────────────────────────────────

    def sync_balances(self) -> None:
        """Sync wallet balances for all active accounts.

        For each active account, creates a temporary ExchangeClient with
        decrypted API credentials, fetches the USDT balance, and updates
        ``cached_balance`` and ``last_sync_at`` in the database.
        """
        accounts = self._get_active_accounts()
        if not accounts:
            logger.debug('No active accounts to sync balances for')
            return

        logger.info('Syncing balances for %d account(s)', len(accounts))

        for account in accounts:
            try:
                client = self._create_exchange_client(account)
                client.initialize()

                balance_info = client.fetch_balance()
                total_balance = balance_info.get('total', 0.0)

                with self._db.session() as session:
                    acct = session.get(AccountModel, account.id)
                    if acct:
                        acct.cached_balance = total_balance
                        acct.last_sync_at = datetime.now(timezone.utc)

                logger.info(
                    'Balance synced for account_id=%d: %.4f USDT',
                    account.id,
                    total_balance,
                )
            except Exception:
                logger.exception(
                    'Failed to sync balance for account_id=%d', account.id
                )

    # ───────────────────────────────────────────
    # Position Sync
    # ───────────────────────────────────────────

    def sync_positions(self) -> None:
        """Sync open positions from the exchange for all active accounts.

        Fetches current positions from Binance, inserts new ones into
        the database, updates existing open positions, and marks positions
        that are no longer reported by the exchange as ``closed``.
        """
        accounts = self._get_active_accounts()
        if not accounts:
            logger.debug('No active accounts to sync positions for')
            return

        logger.info('Syncing positions for %d account(s)', len(accounts))

        for account in accounts:
            try:
                client = self._create_exchange_client(account)
                client.initialize()

                exchange_positions = client.fetch_positions()
                self._reconcile_positions(account.id, exchange_positions)

                logger.info(
                    'Positions synced for account_id=%d: %d open on exchange',
                    account.id,
                    len(exchange_positions),
                )
            except Exception:
                logger.exception(
                    'Failed to sync positions for account_id=%d', account.id
                )

    def _reconcile_positions(
        self, account_id: int, exchange_positions: List[dict]
    ) -> None:
        """Reconcile exchange positions with database records.

        Args:
            account_id: The account to reconcile for.
            exchange_positions: List of position dicts from ExchangeClient.
        """
        # Build a set of (symbol, side) tuples currently on exchange
        exchange_keys: set = set()
        for pos in exchange_positions:
            symbol = pos.get('symbol', '')
            side = pos.get('side', '').lower()
            exchange_keys.add((symbol, side))

        with self._db.session() as session:
            # Fetch all currently-open positions in DB for this account
            db_positions: List[PositionModel] = (
                session.query(PositionModel)
                .filter(
                    PositionModel.account_id == account_id,
                    PositionModel.status == 'open',
                )
                .all()
            )

            db_keys: set = set()
            db_map: dict = {}
            for db_pos in db_positions:
                key = (db_pos.symbol, db_pos.side)
                db_keys.add(key)
                db_map[key] = db_pos

            # Update existing or insert new positions
            for pos in exchange_positions:
                symbol = pos.get('symbol', '')
                side = pos.get('side', '').lower()
                key = (symbol, side)

                if key in db_map:
                    # Update existing position
                    db_pos = db_map[key]
                    db_pos.quantity = pos.get('contracts', 0.0)
                    db_pos.entry_price = pos.get('entryPrice', 0.0)
                    db_pos.unrealized_pnl = pos.get('unrealizedPnl', 0.0)
                    db_pos.leverage = pos.get('leverage', 1)
                    db_pos.liquidation_price = pos.get('liquidationPrice')
                else:
                    # Insert new position
                    new_pos = PositionModel(
                        account_id=account_id,
                        symbol=symbol,
                        side=side,
                        quantity=pos.get('contracts', 0.0),
                        entry_price=pos.get('entryPrice', 0.0),
                        unrealized_pnl=pos.get('unrealizedPnl', 0.0),
                        leverage=pos.get('leverage', 1),
                        liquidation_price=pos.get('liquidationPrice'),
                        status='open',
                    )
                    session.add(new_pos)

            # Mark positions closed if no longer on exchange
            closed_keys = db_keys - exchange_keys
            for key in closed_keys:
                db_pos = db_map[key]
                db_pos.status = 'closed'
                db_pos.closed_at = datetime.now(timezone.utc)
                logger.info(
                    'Position closed: account_id=%d symbol=%s side=%s',
                    account_id,
                    db_pos.symbol,
                    db_pos.side,
                )

    # ───────────────────────────────────────────
    # Trade Sync
    # ───────────────────────────────────────────

    def sync_trades(self) -> None:
        """Sync trade history for all active accounts.

        Trade reconciliation against the exchange is complex and
        requires careful de-duplication against locally recorded
        basket closes. This method logs the intent; full implementation
        will follow once the reconciliation strategy is finalised.
        """
        accounts = self._get_active_accounts()
        if not accounts:
            logger.debug('No active accounts to sync trades for')
            return

        logger.info(
            'Trade sync requested for %d account(s) — '
            'trade reconciliation is complex and handled by the execution '
            'engine at basket close time. Skipping exchange-side pull.',
            len(accounts),
        )

    # ───────────────────────────────────────────
    # Risk Metrics Update
    # ───────────────────────────────────────────

    def update_risk_metrics(self) -> None:
        """Compute and store a risk-metrics snapshot for each active account.

        Calculates:
        - **daily_loss**: Realised PnL since the start of the current UTC day.
        - **max_drawdown**: Balance decline from the high-water mark.
        - **current_exposure**: Total margin in open positions.
        - **open_positions_count**: Number of open positions.
        """
        accounts = self._get_active_accounts()
        if not accounts:
            logger.debug('No active accounts to update risk metrics for')
            return

        logger.info(
            'Updating risk metrics for %d account(s)', len(accounts)
        )

        for account in accounts:
            try:
                self._compute_and_save_risk_metrics(account)
                logger.info(
                    'Risk metrics updated for account_id=%d', account.id
                )
            except Exception:
                logger.exception(
                    'Failed to update risk metrics for account_id=%d',
                    account.id,
                )

    def _compute_and_save_risk_metrics(self, account: AccountModel) -> None:
        """Compute risk metrics for a single account and save to DB.

        Args:
            account: The AccountModel to compute metrics for.
        """
        current_balance = account.cached_balance or 0.0

        # Compute daily loss from today's trades
        today_trades = self._db.get_today_trades(account_id=account.id)
        daily_pnl = sum(t.pnl for t in today_trades)
        daily_loss = abs(daily_pnl) if daily_pnl < 0 else 0.0

        # Determine daily start balance
        # Start balance = current balance minus today's realised PnL
        daily_start_balance = current_balance - daily_pnl

        # High-water mark: check the latest risk metric snapshot
        high_water_mark = current_balance
        with self._db.session() as session:
            latest_metric: Optional[RiskMetricModel] = (
                session.query(RiskMetricModel)
                .filter(RiskMetricModel.account_id == account.id)
                .order_by(RiskMetricModel.snapshot_at.desc())
                .first()
            )
            if latest_metric and latest_metric.high_water_mark > high_water_mark:
                high_water_mark = latest_metric.high_water_mark

        # Drawdown from high-water mark
        max_drawdown = 0.0
        if high_water_mark > 0:
            max_drawdown = (high_water_mark - current_balance) / high_water_mark

        # Open positions and exposure
        open_positions = self._db.get_positions(
            account_id=account.id, status='open'
        )
        open_positions_count = len(open_positions)
        current_exposure = sum(
            (pos.quantity * pos.entry_price) / max(pos.leverage, 1)
            for pos in open_positions
        )

        # Persist the snapshot
        self._db.save_risk_metrics(
            account_id=account.id,
            daily_loss=daily_loss,
            max_drawdown=max_drawdown,
            current_exposure=current_exposure,
            high_water_mark=high_water_mark,
            open_positions_count=open_positions_count,
            daily_start_balance=daily_start_balance,
            current_balance=current_balance,
        )

    # ───────────────────────────────────────────
    # Internal Helpers
    # ───────────────────────────────────────────

    def _get_active_accounts(self) -> List[AccountModel]:
        """Retrieve all active accounts from the database.

        Returns:
            List of active AccountModel instances.
        """
        try:
            return self._db.get_active_accounts()
        except Exception:
            logger.exception('Failed to fetch active accounts')
            return []

    def _create_exchange_client(self, account: AccountModel) -> ExchangeClient:
        """Create an ExchangeClient with decrypted credentials for an account.

        Args:
            account: AccountModel with encrypted API key/secret.

        Returns:
            Configured ExchangeClient (not yet initialised).
        """
        api_key = self._encryption.decrypt(account.encrypted_api_key)
        api_secret = self._encryption.decrypt(account.encrypted_api_secret)

        # Build per-account settings with testnet override
        account_settings = Settings.create_account_settings(
            self._settings,
            {
                'risk_pct': account.risk_pct,
                'max_positions': account.max_positions,
                'leverage_override': account.leverage_override,
                'tp_settings': account.tp_settings,
                'sl_settings': account.sl_settings,
            },
        )
        account_settings.use_testnet = account.use_testnet

        return ExchangeClient.for_account(
            settings=account_settings,
            api_key=api_key,
            api_secret=api_secret,
        )
