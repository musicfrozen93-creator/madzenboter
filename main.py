"""
Zentry Futures Core — Entry Point.

CLI interface for live trading and maintenance commands.

Usage:
    python main.py --mode live          # Live trading (testnet by default)
    python main.py --clear-shutdown     # Clear emergency shutdown flag
"""

import argparse
import logging
import sys

from config.settings import Settings
from core.database import Database
from core.engine import TradingEngine

logger = logging.getLogger(__name__)


def main() -> None:
    """Main entry point — parse arguments and dispatch."""
    parser = argparse.ArgumentParser(
        description='Zentry Futures Core — Hybrid Futures Trading Bot',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Live trading (testnet):
    python main.py --mode live

  Backtest:
    python main.py --mode backtest --symbols BTC/USDT:USDT --start 2026-01-01 --end 2026-03-01

  Clear emergency shutdown:
    python main.py --clear-shutdown
        """,
    )

    parser.add_argument(
        '--mode', choices=['live'], default='live',
        help='Run mode: live trading (default: live)',
    )
    parser.add_argument(
        '--config', default='config/config.json',
        help='Path to configuration file (default: config/config.json)',
    )
    parser.add_argument(
        '--clear-shutdown', action='store_true',
        help='Clear emergency shutdown flag and exit',
    )
    parser.add_argument(
        '--api', action='store_true',
        help='Start the Admin REST API server in a background thread',
    )

    args = parser.parse_args()

    # ── Load settings ──
    try:
        settings = Settings.load(args.config)
    except FileNotFoundError:
        print(f'Error: Config file not found: {args.config}')
        print('Copy config/config.json.example to config/config.json and edit it.')
        sys.exit(1)
    except Exception as e:
        print(f'Error loading config: {e}')
        sys.exit(1)

    # ── Clear emergency shutdown ──
    if args.clear_shutdown:
        db = Database(settings.database_url)
        db.initialize()
        db.set_state('emergency_shutdown', 'false')
        db.set_state('emergency_shutdown_reason', '')
        db.set_state('emergency_shutdown_critical', 'false')
        print('✓ Emergency shutdown cleared. You can restart the bot.')
        db.close()
        return

    # ── Live trading ──
    if args.mode == 'live':
        print()
        print('═' * 50)
        print('  ZENTRY FUTURES CORE')
        print(f'  Mode: {"TESTNET" if settings.use_testnet else "⚠️  LIVE TRADING"}')
        print('═' * 50)
        print()

        if not settings.use_testnet:
            print('⚠️  WARNING: Live trading mode is enabled!')
            print('   Make sure you understand the risks.')
            print()

        engine = TradingEngine(settings)
        if args.api:
            engine.start_api_server()
        engine.start()


if __name__ == '__main__':
    main()
