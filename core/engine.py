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
from control.bot_control import BotControl
from core.database import Database
from exchange.client import ExchangeClient
from signals.signal_engine import SignalEngine

logger = logging.getLogger('zentry')


# ─────────────────────────────────────────────
# Account-Aware Log Filter
# ─────────────────────────────────────────────

class AccountFilter(logging.Filter):
    """Injects account_id into all log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, 'account_id'):
            record.account_id = 'SYSTEM'
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
        self._last_signal_eval = 0.0
        self._last_status_log = 0.0
        # Fixed supported-symbol list (no scanner / no dynamic watchlist).
        self._symbols: List[str] = list(settings.supported_symbols)
        # Account-based trading is the ONLY mode. _account_trading_enabled is True
        # once the encryption service is available to decrypt per-user credentials.
        self._account_trading_enabled = False
        self._account_manager = None
        self._signal_executor = None
        self._sync_service = None
        self._api_thread = None

        # Centralized bot control (reads BOT_ENABLED, MANAGE_EXISTING_POSITIONS,
        # FORCE_CLOSE_ALL from env vars; overridable via admin API at runtime).
        self.bot_control = BotControl()

        # Setup logging first
        self._setup_logging()

        # Database (PostgreSQL via SQLAlchemy)
        self.database = Database(settings.database_url)
        self.database.initialize()

        # Exchange client for PUBLIC market data only (signals).
        # Carries NO API keys and cannot place orders. All trading uses
        # per-account clients built from database credentials.
        self.exchange_client = ExchangeClient.for_market_data(settings)

        # Signal engine (read-only market data, fixed supported symbols)
        self.signal_engine = SignalEngine(self.exchange_client, settings)

        # NOTE: There is intentionally NO engine-level (global) RiskManager or
        # PositionManager. All risk state, position sizing, and shutdown state
        # live PER-ACCOUNT inside the SignalExecutor (each account gets its own
        # RiskManager bound to an account-isolated state namespace). This removes
        # any global/shared risk state and any single global shutdown.

        # Per-account trading components (require the encryption key)
        self._init_multi_account()

    # ───────────────────────────────────────────
    # Multi-Account Initialization
    # ───────────────────────────────────────────

    def _init_multi_account(self) -> None:
        """Initialize the database-account trading components.

        The encryption service is REQUIRED to decrypt per-user API credentials.
        Without it the bot cannot trade any account and runs in market-data-only
        mode (it will never fall back to master/VPS keys).
        """
        if not self.settings.master_encryption_key:
            logger.critical(
                'MASTER_ENCRYPTION_KEY not set — cannot decrypt user account '
                'credentials. The bot will run in MARKET-DATA-ONLY mode and will '
                'NOT trade. Set MASTER_ENCRYPTION_KEY to enable account trading.'
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
                bot_control=self.bot_control,
            )
            self._sync_service = SyncService(
                db=self.database,
                account_manager=self._account_manager,
                encryption=self._encryption,
                settings=self.settings,
            )
            self._account_trading_enabled = True
            logger.info('Database-account trading ENABLED (per-user isolated execution)')

        except ImportError as e:
            logger.critical(
                'Account-trading dependencies not available: %s — '
                'running in market-data-only mode (NO trading).', e,
            )
        except Exception as e:
            logger.critical(
                'Failed to initialize account-trading components: %s — '
                'running in market-data-only mode (NO trading).', e,
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
        console.addFilter(account_filter)
        console.setFormatter(logging.Formatter(log_format, date_format))
        root_logger.addHandler(console)

        # bot.log — main operations
        bot_handler = RotatingFileHandler(
            'logs/bot.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        bot_handler.setLevel(logging.INFO)
        bot_handler.addFilter(account_filter)
        bot_handler.setFormatter(logging.Formatter(log_format, date_format))
        root_logger.addHandler(bot_handler)

        # errors.log — errors only
        error_handler = RotatingFileHandler(
            'logs/errors.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.addFilter(account_filter)
        error_handler.setFormatter(logging.Formatter(log_format, date_format))
        root_logger.addHandler(error_handler)

        # trades.log — separate logger for trade events
        trade_logger = logging.getLogger('trades')
        trade_handler = RotatingFileHandler(
            'logs/trades.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        trade_handler.setLevel(logging.INFO)
        trade_handler.addFilter(account_filter)
        trade_handler.setFormatter(logging.Formatter(log_format, date_format))
        trade_logger.addHandler(trade_handler)

        # execution.log — per-account execution events
        exec_logger = logging.getLogger('execution')
        exec_handler = RotatingFileHandler(
            'logs/execution.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        exec_handler.setLevel(logging.INFO)
        exec_handler.addFilter(account_filter)
        exec_handler.setFormatter(logging.Formatter(log_format, date_format))
        exec_logger.addHandler(exec_handler)

        # accounts.log — account management events
        acct_logger = logging.getLogger('accounts')
        acct_handler = RotatingFileHandler(
            'logs/accounts.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        acct_handler.setLevel(logging.INFO)
        acct_handler.addFilter(account_filter)
        acct_handler.setFormatter(logging.Formatter(log_format, date_format))
        acct_logger.addHandler(acct_handler)

        # control.log — dedicated [CONTROL] plane events (bot start/stop,
        # emergency stop, force-close, position management toggles).
        # Propagation is kept True so events also appear in bot.log.
        control_log_handler = RotatingFileHandler(
            'logs/control.log', maxBytes=5 * 1024 * 1024, backupCount=10,
            encoding='utf-8',
        )
        control_log_handler.setLevel(logging.INFO)
        control_log_handler.addFilter(account_filter)
        control_log_handler.setFormatter(logging.Formatter(log_format, date_format))
        logging.getLogger('zentry.control').addHandler(control_log_handler)

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
            'DATABASE-ACCOUNTS' if self._account_trading_enabled
            else 'MARKET-DATA-ONLY (no trading)',
        )
        logger.info('=' * 60)

        # Initialise the PUBLIC market-data client (loads markets; no keys).
        try:
            self.exchange_client.initialize()
        except Exception as e:
            logger.critical('Failed to initialise market-data client: %s', e)
            return

        # ── Load trading accounts from the database (single source of truth) ──
        # No master balance, no master risk state, no minimum-balance gate.
        if not self._account_trading_enabled:
            logger.warning(
                'Account trading is DISABLED (no encryption service). The engine '
                'will scan markets but place no trades.'
            )
        else:
            try:
                active = self._account_manager.get_active_accounts()
                tradeable = self.database.get_tradeable_accounts()
                if not active:
                    logger.warning(
                        'No active accounts found — starting successfully. The bot '
                        'will NOT trade until users connect accounts via the website.'
                    )
                else:
                    logger.info(
                        'Loaded %d active account(s); %d currently tradeable '
                        '(active user + active subscription).',
                        len(active), len(tradeable),
                    )
            except Exception as e:
                logger.error('Failed to load accounts at startup: %s', e)

        # Fixed supported-symbol universe (no scanner / dynamic watchlist).
        self._symbols = list(self.settings.supported_symbols)
        logger.info(
            'Trading universe (fixed): %s | timeframe=%s',
            ', '.join(self._symbols), self.settings.timeframe,
        )

        # Validate settings
        issues = self.settings.validate()
        if issues:
            for issue in issues:
                logger.warning('Config issue: %s', issue)

        # ── Startup TP-lock audit: report state, then clean orphaned locks ──
        # A TP lock that survived a crash/restart but whose basket already closed
        # (e.g. finalized via reconcile) must be released so it can never wedge.
        try:
            report = self.database.tp_lock_consistency_report()
            if report:
                logger.info('TP_LOCK_REPORT | %d tracked TP lock(s) at startup:', len(report))
                for r in report:
                    logger.info(
                        '  account=%s basket=%s lock=%s basket_state=%s trade_exists=%s',
                        r['account_id'], (r['basket_id'] or '')[:8], r['tp_lock_state'],
                        r['basket_state'], r['trade_record_exists'],
                    )
            cleaned = self.database.cleanup_orphan_tp_locks()
            if cleaned:
                logger.warning(
                    'TP_LOCK_CLEANUP | released %d orphaned TP lock(s) on '
                    'closed/missing baskets at startup.', len(cleaned),
                )
                for c in cleaned:
                    logger.warning('  cleared %s (basket %s)', c['key'], c['basket_id'][:8])
        except Exception as e:
            logger.error('Startup TP-lock audit failed: %s', e)

        # Start background services (multi-account mode)
        if self._sync_service:
            self._sync_service.start()
            logger.info('Background sync service started')

        self._running = True
        logger.info('Trading engine started — entering main loop')

        self._run_loop()

    def _run_loop(self) -> None:
        """Main trading loop — purely database-account driven.

        The engine itself holds NO trading balance, NO global risk state, and
        NO global shutdown. It only: (1) refreshes shared market data, then
        (2) delegates per-account management and signal execution to the
        SignalExecutor, where each account runs as an independent entity.
        """
        while self._running:
            loop_start = time.time()

            try:
                trading = bool(self._account_trading_enabled and self._signal_executor)

                # ── 1. EXITS FIRST (top priority) ──
                # Manage existing positions for EVERY active account BEFORE any
                # scanning or signal generation. Exit conditions (TP / SL /
                # profit-protection / emergency) must always take priority over
                # new entries, so this runs first and on every loop iteration —
                # never starved by, or waiting behind, the signal phase below.
                # Accounts are managed concurrently inside the executor.
                #
                # CONTROL GATE: manage_existing_positions=False → skip management
                # (monitoring-only mode). TP/SL still run when True.
                if trading:
                    if self.bot_control.can_manage_positions():
                        try:
                            self._signal_executor.manage_all_accounts()
                        except Exception as e:
                            logger.error('Per-account basket management error: %s', e)
                    else:
                        logger.debug(
                            '[CONTROL] Position management disabled — monitoring only'
                        )

                # ── 2. If account trading is disabled, never trade. ──
                self.bot_control.set_scanner_running(
                    self.bot_control.can_open_trades() and trading
                )
                if not trading:
                    self._log_status()
                    elapsed = time.time() - loop_start
                    time.sleep(max(1.0, self.settings.loop_interval_seconds - elapsed))
                    continue

                # ── 3. NEW ENTRIES (lower priority, throttled) ──
                # Generating signals for the fixed symbol set is the slowest
                # phase. It is throttled to signal_eval_interval_seconds so the
                # exit cycle above keeps running on the tight loop cadence and is
                # never delayed waiting for new-entry evaluation.
                #
                # CONTROL GATE: signals only when bot fully enabled.
                if (
                    self.bot_control.can_open_trades()
                    and time.time() - self._last_signal_eval >= self.settings.signal_eval_interval_seconds
                ):
                    self._last_signal_eval = time.time()
                    self._evaluate_signals()

                # ── 4. Log periodic status ──
                self._log_status()

            except Exception as e:
                logger.error('Main loop error: %s\n%s', e, traceback.format_exc())
                time.sleep(5)

            # Sleep for remainder of interval
            elapsed = time.time() - loop_start
            sleep_time = max(1.0, self.settings.loop_interval_seconds - elapsed)
            time.sleep(sleep_time)

    def _evaluate_signals(self) -> None:
        """Generate signals for the fixed symbol set and fan each out.

        Strictly LOWER priority than exit management (which always runs first in
        the loop). Per-account eligibility (subscription) and per-account risk
        limits decide independently whether each account takes a signal.
        """
        if not self._symbols:
            logger.warning('No supported symbols configured — nothing to evaluate.')
            return

        evaluated = 0
        found = 0
        for symbol in self._symbols:
            try:
                evaluated += 1
                sig = self.signal_engine.generate_signal(symbol)
                if not sig:
                    continue  # signal_engine logs SIGNAL_SKIP with the reason
                found += 1
                results = self._signal_executor.execute_signal(sig)
                if results:
                    success_count = sum(1 for r in results if r.success)
                    logger.info(
                        'Signal %s %s fanned out — %d/%d eligible accounts handled',
                        sig.side.upper(), symbol, success_count, len(results),
                    )
            except Exception as e:
                logger.debug('Signal error for %s: %s', symbol, e)

        if evaluated:
            logger.info(
                'SIGNAL_FUNNEL | symbols=%d evaluated=%d signals_found=%d '
                'skipped=%d (per-account accept/reject logged above)',
                len(self._symbols), evaluated, found, evaluated - found,
            )

    def _log_status(self) -> None:
        """Log periodic engine status every 5 minutes (account-based).

        Reports active vs tradeable account counts — there is no global bot
        balance or global risk state to report; those live per-account.
        """
        now = time.time()
        if now - self._last_status_log < 300:
            return
        self._last_status_log = now

        active = tradeable = '?'
        if self._account_trading_enabled and self._account_manager:
            try:
                active = len(self._account_manager.get_active_accounts())
                tradeable = len(self.database.get_tradeable_accounts())
            except Exception as e:
                logger.debug('Status account count failed: %s', e)

        mode = 'DATABASE-ACCOUNTS' if self._account_trading_enabled else 'MARKET-DATA-ONLY'
        logger.info(
            'STATUS | mode=%s | accounts active=%s tradeable=%s | symbols=%d',
            mode, active, tradeable, len(self._symbols),
        )

    def start_api_server(self) -> None:
        """Start the admin API server in a background thread."""
        try:
            import uvicorn
            from admin.app import create_app

            app = create_app(
                database=self.database,
                admin_api_key=self.settings.admin_api_key,
                bot_control=self.bot_control,
                signal_executor=self._signal_executor,
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
