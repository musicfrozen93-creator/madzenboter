"""
Zentry Futures Core — Entry Point.

CLI interface for live trading, backtesting, and maintenance commands.

Usage:
    python main.py --mode live          # Live trading (testnet by default)
    python main.py --mode backtest \\
        --symbols BTC/USDT:USDT ETH/USDT:USDT \\
        --start 2026-01-01 --end 2026-03-01 \\
        --balance 100
    python main.py --clear-shutdown     # Clear emergency shutdown flag
"""

import argparse
import logging
import sys

from backtest.data_loader import DataLoader
from backtest.engine import BacktestEngine
from backtest.reporter import BacktestReporter
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
        '--mode', choices=['live', 'backtest'], default='live',
        help='Run mode: live trading or backtesting (default: live)',
    )
    parser.add_argument(
        '--config', default='config/config.json',
        help='Path to configuration file (default: config/config.json)',
    )
    parser.add_argument(
        '--symbols', nargs='+',
        help='Symbols for backtesting (e.g., BTC/USDT:USDT ETH/USDT:USDT)',
    )
    parser.add_argument(
        '--start',
        help='Backtest start date (YYYY-MM-DD)',
    )
    parser.add_argument(
        '--end',
        help='Backtest end date (YYYY-MM-DD)',
    )
    parser.add_argument(
        '--balance', type=float, default=100.0,
        help='Initial balance for backtesting (default: 100.0)',
    )
    parser.add_argument(
        '--clear-shutdown', action='store_true',
        help='Clear emergency shutdown flag and exit',
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
        db = Database('data/zentry.db')
        db.initialize()
        db.set_state('emergency_shutdown', 'false')
        db.set_state('emergency_shutdown_reason', '')
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
        engine.start()

    # ── Backtesting ──
    elif args.mode == 'backtest':
        if not args.symbols:
            print('Error: --symbols is required for backtesting')
            print('Example: --symbols BTC/USDT:USDT ETH/USDT:USDT')
            sys.exit(1)
        if not args.start or not args.end:
            print('Error: --start and --end are required for backtesting')
            print('Example: --start 2026-01-01 --end 2026-03-01')
            sys.exit(1)

        # Setup basic logging for backtest
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )

        print()
        print('═' * 50)
        print('  ZENTRY FUTURES CORE — BACKTEST')
        print(f'  Symbols: {", ".join(args.symbols)}')
        print(f'  Period:  {args.start} → {args.end}')
        print(f'  Balance: ${args.balance:.2f}')
        print('═' * 50)
        print()

        data_loader = DataLoader()
        bt_engine = BacktestEngine(settings, initial_balance=args.balance)

        try:
            metrics = bt_engine.run(args.symbols, args.start, args.end, data_loader)
        except KeyboardInterrupt:
            print('\nBacktest interrupted.')
            sys.exit(0)
        finally:
            data_loader.close()

        if metrics:
            reporter = BacktestReporter()
            reporter.print_report(metrics)

            report_file = f'data/backtest_{args.start}_{args.end}.json'
            reporter.save_report(metrics, report_file)
            print(f'Report saved to: {report_file}')
        else:
            print('No metrics generated — check if data was loaded.')


if __name__ == '__main__':
    main()
