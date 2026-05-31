"""
Zentry Futures Core — SQLite Database Repository.

Provides typed persistence for baskets, trades, watchlist, bot state,
and daily statistics. Uses WAL mode for concurrent read/write performance.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Optional

from core.models import Basket, CoinScore, RecoveryLayer, TradeRecord

logger = logging.getLogger(__name__)


class Database:
    """SQLite repository for all bot persistence needs.

    Thread-safe with check_same_thread=False.
    Uses WAL journal mode for better concurrency.
    """

    def __init__(self, db_path: str = 'data/zentry.db') -> None:
        """Initialise database connection.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA foreign_keys=ON')

    # ───────────────────────────────────────────
    # Schema Initialisation
    # ───────────────────────────────────────────

    def initialize(self) -> None:
        """Create all tables if they do not exist."""
        cursor = self.conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS baskets (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                atr_at_entry REAL NOT NULL,
                volatility TEXT NOT NULL,
                leverage INTEGER NOT NULL DEFAULT 10,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recovery_layers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                basket_id TEXT NOT NULL,
                layer_number INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                margin REAL NOT NULL,
                quantity REAL NOT NULL,
                side TEXT NOT NULL,
                timestamp REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                FOREIGN KEY (basket_id) REFERENCES baskets(id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                basket_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity REAL NOT NULL,
                margin REAL NOT NULL,
                leverage INTEGER NOT NULL,
                pnl REAL NOT NULL,
                fee REAL NOT NULL,
                layers_used INTEGER NOT NULL,
                entry_time REAL NOT NULL,
                exit_time REAL NOT NULL,
                exit_reason TEXT NOT NULL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watchlist (
                symbol TEXT PRIMARY KEY,
                volume_24h REAL,
                atr REAL,
                atr_score REAL,
                volume_score REAL,
                spread_score REAL,
                funding_rate REAL,
                funding_score REAL,
                composite_score REAL,
                updated_at REAL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                starting_balance REAL,
                ending_balance REAL,
                realized_pnl REAL DEFAULT 0,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                max_drawdown REAL DEFAULT 0,
                created_at REAL
            )
        ''')

        # Indexes
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_layers_basket ON recovery_layers(basket_id)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_baskets_status ON baskets(status)'
        )

        self.conn.commit()
        logger.info('Database initialised at %s', self.db_path)

    # ───────────────────────────────────────────
    # Basket Operations
    # ───────────────────────────────────────────

    def save_basket(self, basket: Basket) -> None:
        """Insert a new basket and all its layers.

        Args:
            basket: The Basket to persist.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            '''INSERT OR REPLACE INTO baskets
               (id, symbol, side, atr_at_entry, volatility, leverage, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (basket.id, basket.symbol, basket.side, basket.atr_at_entry,
             basket.volatility, basket.leverage, basket.status, basket.created_at)
        )
        for layer in basket.layers:
            cursor.execute(
                '''INSERT INTO recovery_layers
                   (basket_id, layer_number, entry_price, margin, quantity, side, timestamp, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (basket.id, layer.layer_number, layer.entry_price, layer.margin,
                 layer.quantity, layer.side, layer.timestamp, layer.status)
            )
        self.conn.commit()

    def update_basket(self, basket: Basket) -> None:
        """Update an existing basket and upsert its layers.

        Args:
            basket: The Basket with updated state.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            '''UPDATE baskets SET status=?, leverage=? WHERE id=?''',
            (basket.status, basket.leverage, basket.id)
        )
        # Delete existing layers and re-insert
        cursor.execute('DELETE FROM recovery_layers WHERE basket_id=?', (basket.id,))
        for layer in basket.layers:
            cursor.execute(
                '''INSERT INTO recovery_layers
                   (basket_id, layer_number, entry_price, margin, quantity, side, timestamp, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (basket.id, layer.layer_number, layer.entry_price, layer.margin,
                 layer.quantity, layer.side, layer.timestamp, layer.status)
            )
        self.conn.commit()

    def load_active_baskets(self) -> List[Basket]:
        """Load all baskets with status == 'active', including their layers.

        Returns:
            List of active Basket instances.
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM baskets WHERE status='active'")
        rows = cursor.fetchall()
        baskets: List[Basket] = []
        for row in rows:
            basket = Basket(
                symbol=row['symbol'],
                side=row['side'],
                atr_at_entry=row['atr_at_entry'],
                volatility=row['volatility'],
                id=row['id'],
                created_at=row['created_at'],
                status=row['status'],
                leverage=row['leverage'],
            )
            # Load layers
            cursor.execute(
                'SELECT * FROM recovery_layers WHERE basket_id=? ORDER BY layer_number',
                (basket.id,)
            )
            for lr in cursor.fetchall():
                layer = RecoveryLayer(
                    layer_number=lr['layer_number'],
                    entry_price=lr['entry_price'],
                    margin=lr['margin'],
                    quantity=lr['quantity'],
                    side=lr['side'],
                    timestamp=lr['timestamp'],
                    status=lr['status'],
                )
                basket.layers.append(layer)
            baskets.append(basket)
        return baskets

    def close_basket(self, basket_id: str) -> None:
        """Mark a basket and all its layers as closed.

        Args:
            basket_id: The basket UUID to close.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE baskets SET status='closed' WHERE id=?", (basket_id,)
        )
        cursor.execute(
            "UPDATE recovery_layers SET status='closed' WHERE basket_id=?", (basket_id,)
        )
        self.conn.commit()

    # ───────────────────────────────────────────
    # Trade Operations
    # ───────────────────────────────────────────

    def save_trade(self, trade: TradeRecord) -> None:
        """Insert an immutable trade record.

        Args:
            trade: The TradeRecord to persist.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            '''INSERT INTO trades
               (id, basket_id, symbol, side, entry_price, exit_price, quantity,
                margin, leverage, pnl, fee, layers_used, entry_time, exit_time, exit_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (trade.id, trade.basket_id, trade.symbol, trade.side,
             trade.entry_price, trade.exit_price, trade.quantity,
             trade.margin, trade.leverage, trade.pnl, trade.fee,
             trade.layers_used, trade.entry_time, trade.exit_time, trade.exit_reason)
        )
        self.conn.commit()

    def get_trades_since(self, timestamp: float) -> List[TradeRecord]:
        """Fetch all trades with exit_time >= timestamp.

        Args:
            timestamp: Unix timestamp to filter from.

        Returns:
            List of TradeRecord instances.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT * FROM trades WHERE exit_time >= ? ORDER BY exit_time', (timestamp,)
        )
        return [self._row_to_trade(r) for r in cursor.fetchall()]

    def get_today_trades(self) -> List[TradeRecord]:
        """Fetch all trades from the current UTC day.

        Returns:
            List of today's TradeRecord instances.
        """
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        return self.get_trades_since(today_start)

    # ───────────────────────────────────────────
    # Watchlist Operations
    # ───────────────────────────────────────────

    def save_watchlist(self, scores: List[CoinScore]) -> None:
        """Replace the entire watchlist with new scores.

        Args:
            scores: List of CoinScore entries.
        """
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM watchlist')
        now = time.time()
        for s in scores:
            cursor.execute(
                '''INSERT INTO watchlist
                   (symbol, volume_24h, atr, atr_score, volume_score,
                    spread_score, funding_rate, funding_score, composite_score, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (s.symbol, s.volume_24h, s.atr, s.atr_score, s.volume_score,
                 s.spread_score, s.funding_rate, s.funding_score, s.composite_score, now)
            )
        self.conn.commit()

    def get_watchlist(self) -> List[CoinScore]:
        """Load the current watchlist ordered by composite score.

        Returns:
            List of CoinScore entries, highest score first.
        """
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM watchlist ORDER BY composite_score DESC')
        return [
            CoinScore(
                symbol=r['symbol'],
                volume_24h=r['volume_24h'],
                atr=r['atr'],
                atr_score=r['atr_score'],
                volume_score=r['volume_score'],
                spread_score=r['spread_score'],
                funding_rate=r['funding_rate'],
                funding_score=r['funding_score'],
                composite_score=r['composite_score'],
            )
            for r in cursor.fetchall()
        ]

    # ───────────────────────────────────────────
    # Bot State (Key-Value Store)
    # ───────────────────────────────────────────

    def set_state(self, key: str, value: str) -> None:
        """Upsert a key-value pair into bot_state.

        Args:
            key: State key.
            value: State value (always stored as string).
        """
        cursor = self.conn.cursor()
        cursor.execute(
            '''INSERT OR REPLACE INTO bot_state (key, value, updated_at)
               VALUES (?, ?, ?)''',
            (key, value, time.time())
        )
        self.conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        """Retrieve a value from bot_state.

        Args:
            key: State key to look up.

        Returns:
            The value string, or None if key does not exist.
        """
        cursor = self.conn.cursor()
        cursor.execute('SELECT value FROM bot_state WHERE key=?', (key,))
        row = cursor.fetchone()
        return row['value'] if row else None

    # ───────────────────────────────────────────
    # Daily Statistics
    # ───────────────────────────────────────────

    def save_daily_stats(self, stats: dict) -> None:
        """Insert or replace daily statistics.

        Args:
            stats: Dict with keys: date, starting_balance, ending_balance,
                   realized_pnl, total_trades, winning_trades, losing_trades,
                   max_drawdown.
        """
        cursor = self.conn.cursor()
        cursor.execute(
            '''INSERT OR REPLACE INTO daily_stats
               (date, starting_balance, ending_balance, realized_pnl,
                total_trades, winning_trades, losing_trades, max_drawdown, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (stats.get('date'), stats.get('starting_balance', 0),
             stats.get('ending_balance', 0), stats.get('realized_pnl', 0),
             stats.get('total_trades', 0), stats.get('winning_trades', 0),
             stats.get('losing_trades', 0), stats.get('max_drawdown', 0),
             time.time())
        )
        self.conn.commit()

    # ───────────────────────────────────────────
    # Helpers
    # ───────────────────────────────────────────

    def _row_to_trade(self, row: sqlite3.Row) -> TradeRecord:
        """Convert a database row to a TradeRecord.

        Args:
            row: sqlite3.Row from trades table.

        Returns:
            TradeRecord instance.
        """
        return TradeRecord(
            id=row['id'],
            basket_id=row['basket_id'],
            symbol=row['symbol'],
            side=row['side'],
            entry_price=row['entry_price'],
            exit_price=row['exit_price'],
            quantity=row['quantity'],
            margin=row['margin'],
            leverage=row['leverage'],
            pnl=row['pnl'],
            fee=row['fee'],
            layers_used=row['layers_used'],
            entry_time=row['entry_time'],
            exit_time=row['exit_time'],
            exit_reason=row['exit_reason'],
        )

    def close(self) -> None:
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            logger.info('Database connection closed')
