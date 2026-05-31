"""
Zentry Futures Core — Backtest Data Loader.

Fetches historical OHLCV data from Binance public API via CCXT
and caches it in a local SQLite database to avoid redundant downloads.
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import ccxt
import pandas as pd

logger = logging.getLogger(__name__)


class DataLoader:
    """Fetches and caches historical OHLCV data for backtesting.

    Data is cached in a local SQLite database to avoid repeated
    API calls for the same date ranges.
    """

    def __init__(self, cache_dir: str = 'data') -> None:
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_path = os.path.join(cache_dir, 'backtest_cache.db')
        self.conn = sqlite3.connect(self.cache_path)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self._init_cache()

        # Public CCXT client (no API keys needed for OHLCV)
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'},
        })

    def _init_cache(self) -> None:
        """Create the cache table if it doesn't exist."""
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, timeframe, timestamp)
            )
        ''')
        self.conn.commit()

    def load_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """Load OHLCV data, fetching from Binance if not cached.

        Args:
            symbol: Trading pair (e.g. 'BTC/USDT:USDT').
            timeframe: Candle interval ('5m', '1h', etc.).
            start_date: Start date 'YYYY-MM-DD'.
            end_date: End date 'YYYY-MM-DD'.

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume.
        """
        start_ts = int(
            datetime.strptime(start_date, '%Y-%m-%d')
            .replace(tzinfo=timezone.utc)
            .timestamp() * 1000
        )
        end_ts = int(
            datetime.strptime(end_date, '%Y-%m-%d')
            .replace(tzinfo=timezone.utc)
            .timestamp() * 1000
        )

        # Check cache
        cached = self._load_from_cache(symbol, timeframe, start_ts, end_ts)
        if not cached.empty:
            ts_min = int(cached['timestamp'].min().timestamp() * 1000)
            ts_max = int(cached['timestamp'].max().timestamp() * 1000)
            if ts_min <= start_ts and ts_max >= end_ts - 86400000:
                logger.info(
                    'Cache hit for %s %s (%s to %s): %d bars',
                    symbol, timeframe, start_date, end_date, len(cached),
                )
                return cached

        # Fetch from exchange
        logger.info(
            'Fetching %s %s from %s to %s ...',
            symbol, timeframe, start_date, end_date,
        )
        all_candles = self._fetch_from_exchange(symbol, timeframe, start_ts, end_ts)

        if not all_candles:
            logger.warning('No data returned for %s %s', symbol, timeframe)
            return pd.DataFrame(
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )

        # Store in cache
        self._save_to_cache(symbol, timeframe, all_candles)

        df = pd.DataFrame(
            all_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        logger.info('Loaded %d bars for %s %s', len(df), symbol, timeframe)
        return df

    def _fetch_from_exchange(
        self, symbol: str, timeframe: str, start_ts: int, end_ts: int
    ) -> list:
        """Fetch OHLCV from Binance in batches of 1000 candles.

        Args:
            symbol: Trading pair.
            timeframe: Candle interval.
            start_ts: Start timestamp in ms.
            end_ts: End timestamp in ms.

        Returns:
            List of [timestamp, open, high, low, close, volume] lists.
        """
        all_candles = []
        since = start_ts
        batch_size = 1000

        while since < end_ts:
            try:
                candles = self.exchange.fetch_ohlcv(
                    symbol, timeframe, since=since, limit=batch_size
                )
                if not candles:
                    break

                for c in candles:
                    if c[0] <= end_ts:
                        all_candles.append(c)

                last_ts = candles[-1][0]
                if last_ts <= since:
                    break
                since = last_ts + 1

                # Rate limiting
                time.sleep(0.2)

            except Exception as e:
                logger.warning('Fetch error at %d: %s — retrying', since, e)
                time.sleep(2)

        return all_candles

    def _load_from_cache(
        self, symbol: str, timeframe: str, start_ts: int, end_ts: int
    ) -> pd.DataFrame:
        """Load cached data from SQLite.

        Args:
            symbol: Trading pair.
            timeframe: Candle interval.
            start_ts: Start timestamp in ms.
            end_ts: End timestamp in ms.

        Returns:
            DataFrame of cached candles.
        """
        cursor = self.conn.execute(
            '''SELECT timestamp, open, high, low, close, volume
               FROM candles
               WHERE symbol=? AND timeframe=? AND timestamp>=? AND timestamp<=?
               ORDER BY timestamp''',
            (symbol, timeframe, start_ts, end_ts),
        )
        rows = cursor.fetchall()
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(
            rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df

    def _save_to_cache(
        self, symbol: str, timeframe: str, candles: list
    ) -> None:
        """Save fetched candles to cache.

        Args:
            symbol: Trading pair.
            timeframe: Candle interval.
            candles: List of raw OHLCV data.
        """
        cursor = self.conn.cursor()
        for c in candles:
            cursor.execute(
                '''INSERT OR REPLACE INTO candles
                   (symbol, timeframe, timestamp, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (symbol, timeframe, c[0], c[1], c[2], c[3], c[4], c[5]),
            )
        self.conn.commit()
        logger.debug('Cached %d candles for %s %s', len(candles), symbol, timeframe)

    def close(self) -> None:
        """Close the cache database connection."""
        self.conn.close()
