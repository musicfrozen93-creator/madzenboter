"""Candle cache — avoids refetching the same market data.

Multi-timeframe analysis fetches three timeframes per request plus a benchmark.
Without caching, analysing two pairs a minute apart would refetch 4-hour candles
that cannot possibly have changed.

Two layers:

  • `CandleCache`  — process-wide, TTL scaled to the bar interval. A 4h candle is
                     cached for minutes; a 1m candle for seconds. The TTL is
                     always a small fraction of the bar, so a freshly closed
                     candle is never missed by more than that fraction.
  • `RequestScope` — a per-analysis memo. Within one request the benchmark and
                     any repeated timeframe are fetched exactly once, with no
                     staleness risk at all.

Thread-safe. Frames are returned as-is; callers must not mutate them in place
(the analysis layer only reads).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

import pandas as pd

from providers.base import timeframe_seconds

# TTL is this fraction of the bar interval, clamped to the bounds below.
TTL_BAR_FRACTION = 0.1
TTL_MIN_SECONDS = 10.0
TTL_MAX_SECONDS = 300.0

CacheKey = Tuple[str, str, str, int]     # provider, symbol, timeframe, limit


def ttl_for(timeframe: str) -> float:
    """Cache lifetime for a timeframe: a small fraction of its bar interval."""
    seconds = timeframe_seconds(timeframe)
    return max(TTL_MIN_SECONDS, min(TTL_MAX_SECONDS, seconds * TTL_BAR_FRACTION))


@dataclass
class CacheStats:
    """Hit/miss counters, surfaced by /health for operational visibility."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0

    def as_dict(self) -> dict:
        return {
            'hits': self.hits,
            'misses': self.misses,
            'evictions': self.evictions,
            'hit_rate': round(self.hit_rate, 4),
        }


class CandleCache:
    """Process-wide TTL cache for OHLCV frames."""

    def __init__(self, max_entries: int = 256) -> None:
        self._entries: Dict[CacheKey, tuple[float, pd.DataFrame]] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self.stats = CacheStats()

    def get_or_fetch(
        self,
        provider_name: str,
        symbol: str,
        timeframe: str,
        limit: int,
        fetch: Callable[[], pd.DataFrame],
    ) -> pd.DataFrame:
        """Return cached candles when fresh, otherwise fetch and store them.

        The fetch runs OUTSIDE the lock so a slow venue call never blocks other
        symbols; a concurrent duplicate fetch is possible and harmless.
        """
        key: CacheKey = (provider_name, symbol, timeframe, limit)
        now = time.monotonic()

        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] > now:
                self.stats.hits += 1
                return entry[1]

        self.stats.misses += 1
        frame = fetch()

        with self._lock:
            if len(self._entries) >= self._max_entries:
                self._evict_expired(now)
            self._entries[key] = (now + ttl_for(timeframe), frame)
        return frame

    def _evict_expired(self, now: float) -> None:
        """Drop expired entries; if none are expired, drop the soonest to expire."""
        expired = [k for k, (expires, _) in self._entries.items() if expires <= now]
        if not expired and self._entries:
            expired = [min(self._entries, key=lambda k: self._entries[k][0])]
        for key in expired:
            self._entries.pop(key, None)
            self.stats.evictions += 1

    def clear(self) -> None:
        """Drop every entry (used by tests and on provider shutdown)."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# The service-wide instance.
CANDLE_CACHE = CandleCache()


@dataclass
class RequestScope:
    """Per-analysis memo layered over the process-wide cache.

    Guarantees that one `/api/analyze` call fetches each (symbol, timeframe)
    exactly once, however many pipeline stages ask for it.
    """

    cache: CandleCache = field(default=CANDLE_CACHE)
    _local: Dict[CacheKey, pd.DataFrame] = field(default_factory=dict)
    fetches: int = 0

    def candles(
        self,
        provider_name: str,
        symbol: str,
        timeframe: str,
        limit: int,
        fetch: Callable[[], pd.DataFrame],
    ) -> pd.DataFrame:
        """Fetch candles through the request memo, then the TTL cache."""
        key: CacheKey = (provider_name, symbol, timeframe, limit)
        cached = self._local.get(key)
        if cached is not None:
            return cached

        def counted() -> pd.DataFrame:
            self.fetches += 1
            return fetch()

        frame = self.cache.get_or_fetch(provider_name, symbol, timeframe, limit, counted)
        self._local[key] = frame
        return frame

    def memoized(self, key: str, produce: Callable[[], object]) -> object:
        """Memoize any derived value for the duration of one request."""
        marker: CacheKey = ('__derived__', key, '', 0)
        if marker not in self._local:
            self._local[marker] = produce()          # type: ignore[assignment]
        return self._local[marker]
