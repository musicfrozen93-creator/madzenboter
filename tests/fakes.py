"""Shared in-memory fakes for the analysis tests.

`StubProvider` implements the full `MarketDataProvider` contract over generated
candles, so the pipeline and API can be exercised with zero network access.

It is timeframe-aware: `per_timeframe` lets a test give each rung of the
multi-timeframe ladder its own series, which is how conflicting-trend scenarios
are built.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from providers.base import (
    MarketDataProvider,
    Quote,
    SymbolInfo,
    UnknownSymbolError,
)

DEFAULT_SYMBOL = 'BTCUSDT'


def make_candles(
    n: int = 400,
    start: float = 100.0,
    end: float = 200.0,
    volume: float = 1000.0,
    noise: float = 0.05,
    swing_amplitude: float = 0.0,
    swing_period: int = 12,
) -> pd.DataFrame:
    """Generate a standardized OHLCV frame with a deterministic shape.

    `swing_amplitude` superimposes a sine wave on the trend so the series
    produces confirmed pivots — without it a perfectly straight line yields no
    swings, and every structure-derived module correctly reports "undefined".
    """
    i = np.arange(n)
    trend = np.linspace(start, end, n)
    wave = swing_amplitude * np.sin(2 * np.pi * i / swing_period)
    closes = trend + wave + noise * ((i % 2) * 2 - 1)
    # Each candle opens where the previous one closed, so a candle's body has
    # the direction the series actually moved. Deriving open from its OWN close
    # would make every candle bullish regardless of the trend.
    opens = np.concatenate(([closes[0] - noise], closes[:-1]))
    span = abs(noise) + abs(swing_amplitude) * 0.15 + 0.05
    bodies_high = np.maximum(opens, closes)
    bodies_low = np.minimum(opens, closes)
    return pd.DataFrame({
        'timestamp': pd.to_datetime(i * 900_000, unit='ms'),
        'open': opens,
        'high': bodies_high + span,
        'low': bodies_low - span,
        'close': closes,
        'volume': np.full(n, volume, dtype=float),
    })


def make_uptrend(n: int = 400) -> pd.DataFrame:
    """A clean rising market with real pivots."""
    return make_candles(n=n, start=100.0, end=300.0, swing_amplitude=3.0)


def make_downtrend(n: int = 400) -> pd.DataFrame:
    """A clean falling market with real pivots."""
    return make_candles(n=n, start=300.0, end=100.0, swing_amplitude=3.0)


def make_ranging_candles(n: int = 400, level: float = 100.0, amp: float = 2.0) -> pd.DataFrame:
    """Sideways chop — produces a RANGE structure with no directional setup."""
    i = np.arange(n)
    closes = level + amp * np.sin(i / 3.0)
    opens = np.concatenate(([closes[0]], closes[:-1]))
    return pd.DataFrame({
        'timestamp': pd.to_datetime(i * 900_000, unit='ms'),
        'open': opens,
        'high': np.maximum(opens, closes) + 0.35,
        'low': np.minimum(opens, closes) - 0.35,
        'close': closes,
        'volume': np.full(n, 1000.0, dtype=float),
    })


class StubProvider(MarketDataProvider):
    """A fully in-memory market data provider."""

    name = 'stub'
    market = 'test'
    timeframes = ('5m', '15m', '30m', '1h', '4h', '1d', '1w')

    def __init__(
        self,
        candles: Optional[pd.DataFrame] = None,
        benchmark: Optional[pd.DataFrame] = None,
        spread: float = 0.0,
        symbols: tuple[str, ...] = (DEFAULT_SYMBOL, 'ETHUSDT'),
        per_timeframe: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> None:
        self._candles = candles if candles is not None else make_uptrend()
        self._benchmark = benchmark
        self._spread = spread
        self._symbols = symbols
        self._per_timeframe = per_timeframe or {}
        self.initialized = False
        self.closed = False
        self.fetch_calls: list[tuple[str, str]] = []

    # ── Lifecycle ──

    def initialize(self) -> None:
        self.initialized = True

    def close(self) -> None:
        self.closed = True

    # ── Discovery ──

    def list_symbols(self) -> list[str]:
        return sorted(self._symbols)

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        if symbol not in self._symbols:
            raise UnknownSymbolError(f'{symbol} is not listed')
        return SymbolInfo(
            symbol=symbol, base=symbol[:-4], quote='USDT', price_precision=2
        )

    # ── Market data ──

    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        self.ensure_timeframe(timeframe)
        if symbol not in self._symbols:
            raise UnknownSymbolError(f'{symbol} is not listed')
        self.fetch_calls.append((symbol, timeframe))

        if self._benchmark is not None and symbol == self.reference_symbol():
            return self._benchmark
        return self._per_timeframe.get(timeframe, self._candles)

    def fetch_quote(self, symbol: str) -> Quote:
        if symbol not in self._symbols:
            raise UnknownSymbolError(f'{symbol} is not listed')
        last = float(self._candles['close'].iloc[-1])
        half = self._spread / 2.0
        return Quote(
            symbol=symbol,
            last=last,
            bid=last - half,
            ask=last + half,
            spread=self._spread,
            quote_volume_24h=1_000_000_000.0,
        )

    def reference_symbol(self) -> Optional[str]:
        return DEFAULT_SYMBOL if DEFAULT_SYMBOL in self._symbols else None
