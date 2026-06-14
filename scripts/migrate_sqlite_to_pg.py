"""ZenGrid — SQLite to PostgreSQL Migration Script.

Connects to the legacy single-account SQLite database (data/zentry.db),
extracts all historical records, creates a default admin user and master account
in the PostgreSQL database, and migrates the records while fanning out to
the master account ID where applicable.
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Import DB and models
from config.settings import Settings
from core.database import Database
from core.models import (
    AccountModel,
    Base,
    BasketModel,
    BotStateModel,
    DailyStatModel,
    PositionModel,
    RecoveryLayerModel,
    SignalModel,
    TradeModel,
    UserModel,
    WatchlistModel,
)


def migrate_database(sqlite_path: str, pg_url: str, master_api_key: str, master_api_secret: str) -> None:
    """Run the migration from SQLite to PostgreSQL.

    Args:
        sqlite_path: Path to the SQLite database file.
        pg_url: PostgreSQL connection URL.
        master_api_key: Plaintext master key for default account setup.
        master_api_secret: Plaintext master secret for default account setup.
    """
    print(f'Starting database migration...')
    print(f'SQLite Database:   {sqlite_path}')
    print(f'PostgreSQL URL:    {pg_url.split("@")[-1]}')

    # 1. Connect to SQLite
    if not os.path.exists(sqlite_path):
        print(f'SQLite database file not found at {sqlite_path}. Nothing to migrate.')
        sys.exit(0)

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()

    # 2. Connect to PostgreSQL
    pg_engine = create_engine(pg_url)
    # Ensure tables exist
    Base.metadata.create_all(bind=pg_engine)
    PgSession = sessionmaker(bind=pg_engine)
    pg_session = PgSession()

    try:
        # 3. Create default user and master account if they don't exist
        user = pg_session.query(UserModel).filter(UserModel.username == 'admin').first()
        if not user:
            print('Creating default admin user in PostgreSQL...')
            user = UserModel(
                email='admin@zengrid.local',
                username='admin',
                hashed_password='disabled_placeholder',
                is_active=True,
                is_admin=True,
            )
            pg_session.add(user)
            pg_session.flush()

        master_account = pg_session.query(AccountModel).filter(AccountModel.label == 'Master Account').first()
        if not master_account:
            print('Creating default Master Account in PostgreSQL...')
            # Import encryption to encrypt master keys
            master_key = os.environ.get('MASTER_ENCRYPTION_KEY', '')
            if master_key:
                from accounts.encryption import EncryptionService
                enc = EncryptionService(master_key)
                enc_api_key = enc.encrypt(master_api_key or 'placeholder_key')
                enc_api_secret = enc.encrypt(master_api_secret or 'placeholder_secret')
            else:
                enc_api_key = 'encryption_disabled_placeholder'
                enc_api_secret = 'encryption_disabled_placeholder'

            master_account = AccountModel(
                user_id=user.id,
                label='Master Account',
                encrypted_api_key=enc_api_key,
                encrypted_api_secret=enc_api_secret,
                is_active=True,
                use_testnet=True,  # Default to testnet as bot default
                risk_pct=0.02,
                max_positions=5,
            )
            pg_session.add(master_account)
            pg_session.flush()

        account_id = master_account.id

        # Helper to check if SQLite table exists
        def table_exists(name: str) -> bool:
            sqlite_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
            return sqlite_cursor.fetchone() is not None

        # 4. Migrate bot_state
        if table_exists('bot_state'):
            print('Migrating bot_state...')
            sqlite_cursor.execute('SELECT key, value, updated_at FROM bot_state')
            rows = sqlite_cursor.fetchall()
            migrated = 0
            for r in rows:
                # Check if exists in PG
                exists = pg_session.get(BotStateModel, r['key'])
                if not exists:
                    pg_session.add(BotStateModel(
                        key=r['key'],
                        value=r['value'],
                        updated_at=r['updated_at'],
                    ))
                    migrated += 1
            print(f'✓ Migrated {migrated} bot_state records.')

        # 5. Migrate watchlist
        if table_exists('watchlist'):
            print('Migrating watchlist...')
            sqlite_cursor.execute('SELECT symbol, volume_24h, atr, atr_score, volume_score, spread_score, funding_rate, funding_score, composite_score, updated_at FROM watchlist')
            rows = sqlite_cursor.fetchall()
            migrated = 0
            # Clear PostgreSQL watchlist first
            pg_session.query(WatchlistModel).delete()
            for r in rows:
                pg_session.add(WatchlistModel(
                    symbol=r['symbol'],
                    volume_24h=r['volume_24h'],
                    atr=r['atr'],
                    atr_score=r['atr_score'],
                    volume_score=r['volume_score'],
                    spread_score=r['spread_score'],
                    funding_rate=r['funding_rate'],
                    funding_score=r['funding_score'],
                    composite_score=r['composite_score'],
                    updated_at=r['updated_at'],
                ))
                migrated += 1
            print(f'✓ Migrated {migrated} watchlist records.')

        # 6. Migrate baskets and recovery_layers
        if table_exists('baskets'):
            print('Migrating baskets and recovery layers...')
            sqlite_cursor.execute('SELECT id, symbol, side, atr_at_entry, volatility, leverage, status, created_at FROM baskets')
            basket_rows = sqlite_cursor.fetchall()
            b_migrated = 0
            l_migrated = 0
            for br in basket_rows:
                exists = pg_session.get(BasketModel, br['id'])
                if not exists:
                    pg_session.add(BasketModel(
                        id=br['id'],
                        account_id=account_id,
                        symbol=br['symbol'],
                        side=br['side'],
                        atr_at_entry=br['atr_at_entry'],
                        volatility=br['volatility'],
                        leverage=br['leverage'],
                        status=br['status'],
                        created_at=br['created_at'],
                    ))
                    b_migrated += 1

                    # Migrate layers for this basket
                    if table_exists('recovery_layers'):
                        sqlite_cursor.execute(
                            'SELECT layer_number, entry_price, margin, quantity, side, timestamp, status FROM recovery_layers WHERE basket_id=?',
                            (br['id'],)
                        )
                        layer_rows = sqlite_cursor.fetchall()
                        for lr in layer_rows:
                            pg_session.add(RecoveryLayerModel(
                                basket_id=br['id'],
                                layer_number=lr['layer_number'],
                                entry_price=lr['entry_price'],
                                margin=lr['margin'],
                                quantity=lr['quantity'],
                                side=lr['side'],
                                timestamp=lr['timestamp'],
                                status=lr['status'],
                            ))
                            l_migrated += 1
            print(f'✓ Migrated {b_migrated} baskets and {l_migrated} recovery layers.')

        # 7. Migrate trades
        if table_exists('trades'):
            print('Migrating trades...')
            sqlite_cursor.execute('SELECT id, basket_id, symbol, side, entry_price, exit_price, quantity, margin, leverage, pnl, fee, layers_used, entry_time, exit_time, exit_reason FROM trades')
            rows = sqlite_cursor.fetchall()
            migrated = 0
            for r in rows:
                exists = pg_session.get(TradeModel, r['id'])
                if not exists:
                    pg_session.add(TradeModel(
                        id=r['id'],
                        account_id=account_id,
                        basket_id=r['basket_id'],
                        symbol=r['symbol'],
                        side=r['side'],
                        entry_price=r['entry_price'],
                        exit_price=r['exit_price'],
                        quantity=r['quantity'],
                        margin=r['margin'],
                        leverage=r['leverage'],
                        pnl=r['pnl'],
                        fee=r['fee'],
                        layers_used=r['layers_used'],
                        entry_time=r['entry_time'],
                        exit_time=r['exit_time'],
                        exit_reason=r['exit_reason'],
                    ))
                    migrated += 1
            print(f'✓ Migrated {migrated} trade records.')

        # 8. Migrate daily_stats
        if table_exists('daily_stats'):
            print('Migrating daily_stats...')
            sqlite_cursor.execute('SELECT id, date, starting_balance, ending_balance, realized_pnl, total_trades, winning_trades, losing_trades, max_drawdown, created_at FROM daily_stats')
            rows = sqlite_cursor.fetchall()
            migrated = 0
            for r in rows:
                # Check for existing stat in PG
                exists = pg_session.query(DailyStatModel).filter(
                    DailyStatModel.account_id == account_id,
                    DailyStatModel.date == r['date']
                ).first()

                if not exists:
                    pg_session.add(DailyStatModel(
                        account_id=account_id,
                        date=r['date'],
                        starting_balance=r['starting_balance'],
                        ending_balance=r['ending_balance'],
                        realized_pnl=r['realized_pnl'],
                        total_trades=r['total_trades'],
                        winning_trades=r['winning_trades'],
                        losing_trades=r['losing_trades'],
                        max_drawdown=r['max_drawdown'],
                        created_at=r['created_at'],
                    ))
                    migrated += 1
            print(f'✓ Migrated {migrated} daily stats.')

        # Commit all PostgreSQL transactions
        pg_session.commit()
        print('Database migration completed successfully!')

    except Exception as exc:
        pg_session.rollback()
        print(f'🚨 Error occurred during migration: {exc}', file=sys.stderr)
        raise
    finally:
        sqlite_conn.close()
        pg_session.close()


def main() -> None:
    """Parse CLI args and execute migration."""
    parser = argparse.ArgumentParser(
        description='ZenGrid — Legacy SQLite to PostgreSQL Migration Utility',
    )
    parser.add_argument(
        '--sqlite-path', default='data/zentry.db',
        help='Path to the legacy SQLite file (default: data/zentry.db)',
    )
    parser.add_argument(
        '--config', default='config/config.json',
        help='Path to config file for PG database URL (default: config/config.json)',
    )
    args = parser.parse_args()

    # Load settings to get pg connection
    try:
        settings = Settings.load(args.config)
    except Exception as e:
        print(f'Error loading configuration: {e}', file=sys.stderr)
        sys.exit(1)

    # Master Binance keys from env if available
    binance_key = os.environ.get('BINANCE_API_KEY', '')
    binance_secret = os.environ.get('BINANCE_API_SECRET', '')

    migrate_database(
        sqlite_path=args.sqlite_path,
        pg_url=settings.database_url,
        master_api_key=binance_key,
        master_api_secret=binance_secret,
    )


if __name__ == '__main__':
    main()
