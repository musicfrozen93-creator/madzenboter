"""
Zentry Futures Core — Main Trading Engine.

Orchestrates the full trading loop: scanning, signal generation,
position management, risk monitoring, and logging. This is the
central coordinator that ties all modules together.

Extended for multi-account support: signals are fanned out to all
active accounts via the SignalExecutor. Single-account mode is
preserved as a fallback when no accounts are configured.
"""

import logging
import os
import signal as signal_module
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import List

from config.settings import Settings
from core.database import Database
from core.dto import Basket, CoinScore
from exchange.client import ExchangeClient
from grid.position_manager import PositionManager
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from risk.stop_loss import StopLossManager
from scanner.coin_scanner import CoinScanner
from signals.signal_engine import SignalEngine

logger = logging.getLogger('zentry')


# ─────────────────────────────────────────────
# Account-Aware Log Filter
# ─────────────────────────────────────────────

class AccountFilter(logging.Filter):
    """Injects account_id into all log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, 'account_id'):
            record.account_id = 'master'
        return True


class TradingEngine:
    """Main trading engine orchestrating the complete bot lifecycle.

    Initialization → Scan → Signal → Trade → Manage → Repeat

    Handles:
      • Component initialization and wiring
      • Multi-account signal fan-out via SignalExecutor
      • Single-account fallback when no accounts are configured
      • Rotating log file setup
      • Graceful shutdown on SIGINT / SIGTERM
      • Main trading loop with error recovery
      • Daily reset and periodic status logging
      • Optional Admin API server
      • Background sync services
    """

    def __init__(self, settings: Settings) -> None:
        """Initialise all components.

        Args:
            settings: Loaded application settings.
        """
        self.settings = settings
        self._running = False
        self._last_scan_time = 0.0
        self._last_status_log = 0.0
        self._active_baskets: List[Basket] = []
        self._watchlist: List[CoinScore] = []
        self._multi_account_mode = False
        self._signal_executor = None
        self._sync_service = None
        self._api_thread = None

        # Setup logging first
        self._setup_logging()

        # Database (PostgreSQL via SQLAlchemy)
        self.database = Database(settings.database_url)
        self.database.initialize()

        # Exchange (master account)
        self.exchange_client = ExchangeClient(settings)

        # Scanner & Signals
        self.scanner = CoinScanner(self.exchange_client, settings, self.database)
        self.signal_engine = SignalEngine(self.exchange_client, settings)

        # Risk
        self.risk_manager = RiskManager(settings, self.database)
        self.position_sizer = PositionSizer(settings)
        self.sl_manager = StopLossManager(settings)

        # Grid
        self.recovery_system = RecoverySystem(settings)
        self.tp_manager = TakeProfitManager(settings)

        # Position Manager (depends on all above)
        self.position_manager = PositionManager(
            exchange_client=self.exchange_client,
            settings=settings,
            database=self.database,
            risk_manager=self.risk_manager,
            position_sizer=self.position_sizer,
            recovery_system=self.recovery_system,
            tp_manager=self.tp_manager,
            sl_manager=self.sl_manager,
            signal_engine=self.signal_engine,
        )

        # Multi-account components (initialized if encryption key is present)
        self._init_multi_account()

    # ───────────────────────────────────────────
    # Multi-Account Initialization
    # ───────────────────────────────────────────

    def _init_multi_account(self) -> None:
        """Initialize multi-account components if encryption key is configured."""
        if not self.settings.master_encryption_key:
            logger.info(
                'MASTER_ENCRYPTION_KEY not set — running in single-account mode'
            )
            return

        try:
            from accounts.encryption import EncryptionService
            from accounts.manager import AccountManager
            from execution.executor import SignalExecutor
            from services.sync import SyncService

            self._encryption = EncryptionService(self.settings.master_encryption_key)
            self._account_manager = AccountManager(
                db=self.database,
                encryption=self._encryption,
                settings=self.settings,
            )
            self._signal_executor = SignalExecutor(
                db=self.database,
                account_manager=self._account_manager,
                encryption=self._encryption,
                master_settings=self.settings,
            )
            self._sync_service = SyncService(
                db=self.database,
                account_manager=self._account_manager,
                encryption=self._encryption,
                settings=self.settings,
            )

            # Check if any accounts are configured
            accounts = self._account_manager.get_active_accounts()
            if accounts:
                self._multi_account_mode = True
                logger.info(
                    'Multi-account mode ACTIVE — %d accounts loaded', len(accounts)
                )
            else:
                logger.info(
                    'Multi-account mode available but no accounts configured — '
                    'using master account'
                )

        except ImportError as e:
            logger.warning(
                'Multi-account dependencies not available: %s — '
                'running in single-account mode', e,
            )
        except Exception as e:
            logger.error(
                'Failed to initialize multi-account components: %s — '
                'falling back to single-account mode', e,
            )

    # ───────────────────────────────────────────
    # Logging Setup
    # ───────────────────────────────────────────

    def _setup_logging(self) -> None:
        """Configure rotating log files and console output."""
        os.makedirs('logs', exist_ok=True)

        log_format = (
            '%(asctime)s | %(account_id)s | %(name)s | %(levelname)s | %(message)s'
        )
        date_format = '%Y-%m-%d %H:%M:%S'

        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, self.settings.log_level, logging.INFO))

        # Add account filter to root
        account_filter = AccountFilter()
        root_logger.addFilter(account_filter)

        # Clear existing handlers
        root_logger.handlers.clear()

        # Console handler
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter(log_format, date_format))
        root_logger.addHandler(console)

        # bot.log — main operations
        bot_handler = RotatingFileHandler(
            'logs/bot.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        bot_handler.setLevel(logging.INFO)
        bot_handler.setFormatter(logging.Formatter(log_format, date_format))
        root_logger.addHandler(bot_handler)

        # errors.log — errors only
        error_handler = RotatingFileHandler(
            'logs/errors.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(logging.Formatter(log_format, date_format))
        root_logger.addHandler(error_handler)

        # trades.log — separate logger for trade events
        trade_logger = logging.getLogger('trades')
        trade_handler = RotatingFileHandler(
            'logs/trades.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        trade_handler.setLevel(logging.INFO)
        trade_handler.setFormatter(logging.Formatter(log_format, date_format))
        trade_logger.addHandler(trade_handler)

        # execution.log — per-account execution events
        exec_logger = logging.getLogger('execution')
        exec_handler = RotatingFileHandler(
            'logs/execution.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        exec_handler.setLevel(logging.INFO)
        exec_handler.setFormatter(logging.Formatter(log_format, date_format))
        exec_logger.addHandler(exec_handler)

        # accounts.log — account management events
        acct_logger = logging.getLogger('accounts')
        acct_handler = RotatingFileHandler(
            'logs/accounts.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        acct_handler.setLevel(logging.INFO)
        acct_handler.setFormatter(logging.Formatter(log_format, date_format))
        acct_logger.addHandler(acct_handler)

    # ───────────────────────────────────────────
    # Lifecycle
    # ───────────────────────────────────────────

    def start(self) -> None:
        """Start the trading engine.

        Initialises the exchange connection, loads persisted state,
        and enters the main trading loop. Blocks until stop() is called.
        """
        # Register signal handlers
        signal_module.signal(signal_module.SIGINT, self._handle_signal)
        signal_module.signal(signal_module.SIGTERM, self._handle_signal)

        logger.info('=' * 60)
        logger.info('  ZENTRY FUTURES CORE — Starting')
        logger.info('  Testnet: %s', self.settings.use_testnet)
        logger.info(
            '  Mode: %s',
            'MULTI-ACCOUNT' if self._multi_account_mode else 'SINGLE-ACCOUNT',
        )
        logger.info('=' * 60)

        try:
            self.exchange_client.initialize()
        except Exception as e:
            logger.critical('Failed to initialise exchange: %s', e)
            return

        # Fetch initial balance
        try:
            balance_info = self.exchange_client.fetch_balance()
            balance = balance_info['total']
            logger.info('Master account balance: %.2f USDT', balance)
        except Exception as e:
            logger.critical('Failed to fetch balance: %s', e)
            return

        if balance < 5.0 and not self._multi_account_mode:
            logger.critical(
                'Balance too low (%.2f USDT). Minimum recommended: 30 USDT', balance
            )
            return

        # Initialise risk manager
        self.risk_manager.initialize(balance)

        # Check emergency shutdown
        if self.risk_manager.is_emergency_shutdown():
            reason = self.database.get_state('emergency_shutdown_reason') or 'Unknown'
            logger.critical(
                '🚨 Emergency shutdown is ACTIVE (reason: %s). '
                'Run with --clear-shutdown to resume trading.',
                reason,
            )
            return

        # Load persisted baskets
        self._active_baskets = self.database.load_active_baskets()
        if self._active_baskets:
            logger.info('Resumed %d active baskets', len(self._active_baskets))

        # Load persisted watchlist
        self._watchlist = self.database.get_watchlist()

        # Validate settings
        issues = self.settings.validate()
        if issues:
            for issue in issues:
                logger.warning('Config issue: %s', issue)

        # Start background services (multi-account mode)
        if self._sync_service:
            self._sync_service.start()
            logger.info('Background sync service started')

        self._running = True
        logger.info('Trading engine started — entering main loop')

        self._run_loop()

    def _run_loop(self) -> None:
        """Main trading loop."""
        while self._running:
            loop_start = time.time()

            try:
                # 1. Fetch current balance (master account)
                balance_info = self.exchange_client.fetch_balance()
                balance = balance_info['total']

                # 2. Update HWM & daily reset
                self.risk_manager.update_high_water_mark(balance)
                self.risk_manager.record_daily_start(balance)

                # 3. Check daily loss limit (master account)
                if not self._multi_account_mode and self.risk_manager.check_daily_loss_limit(balance):
                    logger.warning(
                        '⚠️ Daily loss limit (5%%) reached! '
                        'Closing all positions and pausing until next UTC day.'
                    )
                    trades = self.position_manager.close_all_baskets(
                        self._active_baskets, 'daily_limit'
                    )
                    self._active_baskets = []
                    logger.info('Closed %d baskets due to daily limit', len(trades))
                    self._wait_until_next_day()
                    continue

                # 4. Check drawdown limit (master account)
                if not self._multi_account_mode and self.risk_manager.check_drawdown_limit(balance):
                    logger.critical(
                        '🚨 MAX DRAWDOWN (15%%) REACHED! '
                        'Emergency shutdown triggered.'
                    )
                    self.position_manager.close_all_baskets(
                        self._active_baskets, 'drawdown'
                    )
                    self._active_baskets = []
                    self.risk_manager.trigger_emergency_shutdown(
                        'Max drawdown exceeded'
                    )
                    self.stop()
                    break

                # 5. Run coin scanner at interval
                if time.time() - self._last_scan_time >= self.settings.scan_interval_seconds:
                    try:
                        self._watchlist = self.scanner.scan()
                        self._last_scan_time = time.time()
                    except Exception as e:
                        logger.error('Scan failed: %s', e)

                # 6. Manage existing positions
                if self._multi_account_mode and self._signal_executor:
                    # Multi-account: manage baskets for all accounts
                    try:
                        self._signal_executor.manage_all_accounts()
                    except Exception as e:
                        logger.error('Multi-account basket management error: %s', e)
                elif self._active_baskets:
                    # Single-account: manage baskets directly
                    self._active_baskets = self.position_manager.manage_baskets(
                        self._active_baskets, balance
                    )

                # 7. Look for new entries
                current_symbols = {b.symbol for b in self._active_baskets}
                max_positions = self.settings.get_max_positions(balance)

                if len(self._active_baskets) < max_positions:
                    for coin in self._watchlist:
                        if coin.symbol in current_symbols:
                            continue
                        if len(self._active_baskets) >= max_positions:
                            break

                        try:
                            sig = self.signal_engine.generate_signal(coin.symbol)
                            if sig:
                                if self._multi_account_mode and self._signal_executor:
                                    # Multi-account: fan out to all accounts
                                    results = self._signal_executor.execute_signal(sig)
                                    success_count = sum(
                                        1 for r in results if r.success
                                    )
                                    if success_count > 0:
                                        current_symbols.add(coin.symbol)
                                        logger.info(
                                            'Signal %s %s executed on %d/%d accounts',
                                            sig.side.upper(), coin.symbol,
                                            success_count, len(results),
                                        )
                                else:
                                    # Single-account: direct execution
                                    basket = self.position_manager.open_position(
                                        sig, balance
                                    )
                                    if basket:
                                        self._active_baskets.append(basket)
                                        current_symbols.add(basket.symbol)
                                        # Refresh balance after entry
                                        balance = self.exchange_client.fetch_balance()['total']
                        except Exception as e:
                            logger.debug('Signal error for %s: %s', coin.symbol, e)

                # 8. Log periodic status
                self._log_status(balance)

            except Exception as e:
                logger.error('Main loop error: %s\n%s', e, traceback.format_exc())
                time.sleep(5)

            # 9. Sleep for remainder of interval
            elapsed = time.time() - loop_start
            sleep_time = max(1.0, self.settings.loop_interval_seconds - elapsed)
            time.sleep(sleep_time)

    def _wait_until_next_day(self) -> None:
        """Sleep until the next UTC day begins."""
        logger.info('Waiting for next UTC day to resume trading...')
        while self._running:
            now = datetime.now(timezone.utc)
            current_day = now.strftime('%Y-%m-%d')
            if current_day != self.risk_manager._current_date:
                logger.info('New UTC day (%s) — resuming trading', current_day)
                balance = self.exchange_client.fetch_balance()['total']
                self.risk_manager.record_daily_start(balance)
                break
            time.sleep(60)

    def _log_status(self, balance: float) -> None:
        """Log periodic status every 5 minutes.

        Args:
            balance: Current account balance.
        """
        now = time.time()
        if now - self._last_status_log < 300:
            return
        self._last_status_log = now

        total_unrealized = 0.0
        basket_info = []
        for basket in self._active_baskets:
            try:
                basket_info.append(
                    f'{basket.symbol}({basket.side[0].upper()}{basket.layer_count}L)'
                )
            except Exception:
                basket_info.append(f'{basket.symbol}(?)')

        mode = 'multi' if self._multi_account_mode else 'single'
        logger.info(
            'STATUS | mode=%s | balance=%.2f USDT | baskets=%d [%s] | watchlist=%d',
            mode, balance, len(self._active_baskets),
            ', '.join(basket_info) if basket_info else 'none',
            len(self._watchlist),
        )

    def start_api_server(self) -> None:
        """Start the admin API server in a background thread."""
        try:
            import uvicorn
            from admin.app import create_app

            app = create_app(
                database=self.database,
                admin_api_key=self.settings.admin_api_key,
            )
            config = uvicorn.Config(
                app,
                host='0.0.0.0',
                port=self.settings.admin_api_port,
                log_level='warning',
            )
            server = uvicorn.Server(config)

            self._api_thread = threading.Thread(
                target=server.run,
                name='admin-api',
                daemon=True,
            )
            self._api_thread.start()
            logger.info(
                'Admin API server started on port %d', self.settings.admin_api_port
            )
        except ImportError as e:
            logger.warning('Admin API dependencies not available: %s', e)
        except Exception as e:
            logger.error('Failed to start admin API: %s', e)

    def stop(self) -> None:
        """Graceful shutdown of the trading engine."""
        logger.info('Shutting down trading engine...')
        self._running = False

        # Stop background services
        if self._sync_service:
            try:
                self._sync_service.stop()
            except Exception:
                pass

        try:
            self.database.close()
        except Exception:
            pass
        logger.info('Trading engine stopped')

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle OS signals (SIGINT, SIGTERM).

        Args:
            signum: Signal number.
            frame: Current stack frame.
        """
        sig_name = signal_module.Signals(signum).name
        logger.info('Received %s — initiating graceful shutdown', sig_name)
        self.stop()
