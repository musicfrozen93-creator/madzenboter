"""Market Data Provider — the abstraction the analysis engine consumes.

This layer is MANDATORY and exists so the Analysis Engine never depends on a
specific exchange. Adding a new market (Bybit, OANDA, MT5 bridge, TradingView,
Polygon, Twelve Data, …) means writing ONE new subclass of `MarketDataProvider`
and registering it — no analysis code changes.

Everything above this layer speaks in the standardized types defined here:

    Candles   a pandas DataFrame with EXACTLY the columns
              ['timestamp', 'open', 'high', 'low', 'close', 'volume'],
              oldest row first, 'timestamp' as tz-naive UTC datetime64[ns]
    Quote     last / bid / ask / spread / 24h quote volume
    SymbolInfo canonical symbol identity + price precision
    Timeframe  the canonical strings in `TIMEFRAMES`

Symbol naming: callers use the PLATFORM symbol form (e.g. 'BTCUSDT', 'EURUSD').
Each provider translates that to and from its own venue form internally
(`BTC/USDT:USDT` for ccxt, `EUR_USD` for OANDA, …). No caller ever sees a
venue-specific symbol.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Sequence

import pandas as pd

# The canonical OHLCV column contract every provider must return.
CANDLE_COLUMNS: tuple[str, ...] = (
    'timestamp', 'open', 'high', 'low', 'close', 'volume',
)

# Canonical timeframe vocabulary. A provider advertises the subset it supports
# via `supported_timeframes()`; anything outside this tuple is rejected before a
# provider is ever called.
TIMEFRAMES: tuple[str, ...] = (
    '1m', '3m', '5m', '15m', '30m',
    '1h', '2h', '4h', '6h', '8h', '12h',
    '1d', '3d', '1w',
)

# Duration of each canonical timeframe in seconds. Used to order timeframes and
# to size cache TTLs against the bar interval.
TIMEFRAME_SECONDS: dict[str, int] = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1_800,
    '1h': 3_600, '2h': 7_200, '4h': 14_400, '6h': 21_600,
    '8h': 28_800, '12h': 43_200,
    '1d': 86_400, '3d': 259_200, '1w': 604_800,
}

# Platform symbols are uppercase alphanumerics, optionally separated by a single
# '_' (e.g. 'BTCUSDT', 'EUR_USD'). Anything else is rejected at the edge so a
# malformed symbol never reaches a provider or an exchange.
_SYMBOL_RE = re.compile(r'^[A-Z0-9]{2,}(?:_[A-Z0-9]{2,})?$')


# ─────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────

class ProviderError(Exception):
    """Base class for every provider-layer failure."""


class UnknownSymbolError(ProviderError):
    """The provider does not offer this symbol."""


class UnsupportedTimeframeError(ProviderError):
    """The provider does not offer this timeframe."""


class MarketDataUnavailableError(ProviderError):
    """The venue was reachable but returned no usable data."""


# ─────────────────────────────────────────────
# Standardized data types
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class SymbolInfo:
    """Canonical identity and precision of one tradable instrument."""

    symbol: str            # platform form, e.g. 'BTCUSDT'
    base: str              # 'BTC'
    quote: str             # 'USDT'
    price_precision: int   # decimal places for rendering prices
    tick_size: Optional[float] = None
    active: bool = True


@dataclass(frozen=True)
class Quote:
    """Latest top-of-book snapshot for one symbol."""

    symbol: str
    last: float
    bid: float
    ask: float
    spread: float
    quote_volume_24h: float = 0.0

    @property
    def spread_pct(self) -> float:
        """Spread as a fraction of last price (0.0 when price is unknown)."""
        return (self.spread / self.last) if self.last > 0 else 0.0


# ─────────────────────────────────────────────
# Validation helpers (shared by every provider)
# ─────────────────────────────────────────────

def normalize_symbol(symbol: str) -> str:
    """Uppercase and validate a platform symbol.

    Raises:
        UnknownSymbolError: If the symbol is empty or malformed.
    """
    canonical = (symbol or '').strip().upper().replace('-', '_').replace('/', '')
    if not _SYMBOL_RE.match(canonical):
        raise UnknownSymbolError(f'Malformed symbol: {symbol!r}')
    return canonical


def normalize_timeframe(timeframe: str) -> str:
    """Lowercase and validate a timeframe against the canonical vocabulary.

    Raises:
        UnsupportedTimeframeError: If the timeframe is not a known interval.
    """
    canonical = (timeframe or '').strip().lower()
    if canonical not in TIMEFRAMES:
        raise UnsupportedTimeframeError(
            f'Unknown timeframe {timeframe!r}. Supported: {", ".join(TIMEFRAMES)}'
        )
    return canonical


def validate_candles(df: pd.DataFrame, symbol: str, min_bars: int = 1) -> pd.DataFrame:
    """Assert AND clean a provider's candles to the standardized contract.

    Guards the boundary so corrupted market data never reaches the indicators.
    The frame is repaired where it can be (duplicate timestamps collapsed,
    rows sorted) and rejected where it cannot (NaN or non-positive OHLC,
    impossible high<low candles). If too little clean data remains, this raises
    so the pipeline returns WAIT rather than analysing garbage.

    Raises:
        MarketDataUnavailableError: If the frame is empty, malformed, or does not
            yield at least `min_bars` clean candles.
    """
    if df is None or df.empty:
        raise MarketDataUnavailableError(f'No candles returned for {symbol}')
    missing = [c for c in CANDLE_COLUMNS if c not in df.columns]
    if missing:
        raise MarketDataUnavailableError(
            f'Provider returned candles for {symbol} missing columns: {missing}'
        )

    clean = df.copy()

    # Coerce OHLCV to numbers; anything unparseable becomes NaN and is dropped.
    for col in ('open', 'high', 'low', 'close', 'volume'):
        clean[col] = pd.to_numeric(clean[col], errors='coerce')

    # Collapse duplicate timestamps (keep the latest) and order chronologically —
    # exchanges occasionally repeat the in-progress candle or return it twice.
    clean = clean.drop_duplicates(subset='timestamp', keep='last')
    clean = clean.sort_values('timestamp').reset_index(drop=True)

    # Drop structurally impossible or corrupt rows.
    price_cols = ['open', 'high', 'low', 'close']
    valid = (
        clean[price_cols].notna().all(axis=1)
        & (clean[price_cols] > 0).all(axis=1)
        & clean['volume'].notna() & (clean['volume'] >= 0)
        & (clean['high'] >= clean['low'])
        & (clean['high'] >= clean['close']) & (clean['high'] >= clean['open'])
        & (clean['low'] <= clean['close']) & (clean['low'] <= clean['open'])
    )
    clean = clean[valid].reset_index(drop=True)

    if len(clean) < min_bars:
        raise MarketDataUnavailableError(
            f'Only {len(clean)} usable candles for {symbol} after removing '
            f'corrupt/duplicate rows; at least {min_bars} are needed'
        )
    return clean


# ─────────────────────────────────────────────
# The provider interface
# ─────────────────────────────────────────────

class MarketDataProvider(ABC):
    """One venue's market data, expressed in standardized types.

    A provider is READ-ONLY by contract. It exposes no order, balance, or
    position capability, and implementations must never accept trading
    credentials — this platform analyses markets and never trades them.

    Implementations are expected to be cheap to construct and to do their
    network setup in `initialize()`, which the service calls once at startup.
    """

    #: Stable identifier used in the registry and in API responses ('binance').
    name: str = ''

    #: The market class this provider serves ('crypto', 'forex', 'stocks', …).
    market: str = ''

    #: Canonical timeframes this provider serves, ascending. Declared on the
    #: CLASS so capability discovery never has to construct or initialize a
    #: provider (and never touches the network) just to answer "what do you
    #: support?". Override `supported_timeframes()` if a venue reports them
    #: dynamically.
    timeframes: tuple[str, ...] = ()

    # ── Lifecycle ──

    @abstractmethod
    def initialize(self) -> None:
        """Perform one-time setup (load markets, authenticate a public session)."""

    def close(self) -> None:
        """Release any held resources. Overriding is optional."""

    # ── Capability discovery ──

    def supported_timeframes(self) -> tuple[str, ...]:
        """Canonical timeframes this provider can serve, in ascending order."""
        return self.timeframes

    @abstractmethod
    def list_symbols(self) -> list[str]:
        """Every platform symbol this provider offers, sorted."""

    @abstractmethod
    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        """Resolve a platform symbol to its canonical identity and precision.

        Raises:
            UnknownSymbolError: If the provider does not offer the symbol.
        """

    # ── Market data ──

    @abstractmethod
    def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 300
    ) -> pd.DataFrame:
        """Fetch OHLCV history, oldest row first.

        Returns:
            DataFrame with exactly ``CANDLE_COLUMNS``.

        Raises:
            UnknownSymbolError, UnsupportedTimeframeError,
            MarketDataUnavailableError.
        """

    @abstractmethod
    def fetch_quote(self, symbol: str) -> Quote:
        """Fetch the latest top-of-book snapshot for a symbol."""

    # ── Optional venue extras ──

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Perpetual funding rate, or None on venues where it has no meaning.

        Spot and FX providers should leave this at the default.
        """
        return None

    def reference_symbol(self) -> Optional[str]:
        """The market's systemic benchmark, used as broad-market context.

        'BTCUSDT' for crypto; a dollar-index proxy for FX; an index for equities.
        Return None when the market has no meaningful benchmark.
        """
        return None

    # ── Shared helpers ──

    def ensure_timeframe(self, timeframe: str) -> str:
        """Validate a timeframe against the canonical list AND this provider.

        Raises:
            UnsupportedTimeframeError: If unknown, or not offered here.
        """
        canonical = normalize_timeframe(timeframe)
        supported = self.supported_timeframes()
        if canonical not in supported:
            raise UnsupportedTimeframeError(
                f'{self.name} does not offer {canonical}. '
                f'Supported: {", ".join(supported)}'
            )
        return canonical

    def describe(self) -> dict:
        """Machine-readable capability summary, surfaced by /api/markets."""
        return {
            'provider': self.name,
            'market': self.market,
            'timeframes': list(self.supported_timeframes()),
        }

    def __repr__(self) -> str:
        return f'<{type(self).__name__} name={self.name!r} market={self.market!r}>'


def sort_timeframes(timeframes: Sequence[str]) -> tuple[str, ...]:
    """Sort timeframes into canonical (ascending duration) order."""
    order = {tf: i for i, tf in enumerate(TIMEFRAMES)}
    return tuple(sorted((t for t in timeframes if t in order), key=lambda t: order[t]))


def timeframe_seconds(timeframe: str) -> int:
    """Duration of a canonical timeframe in seconds.

    Raises:
        UnsupportedTimeframeError: If the timeframe is not canonical.
    """
    return TIMEFRAME_SECONDS[normalize_timeframe(timeframe)]
