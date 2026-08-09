"""Market Data Provider layer.

The mandatory abstraction between the Analysis Engine and any specific venue.
The engine consumes `MarketDataProvider`; it never imports an exchange client.

Adding a market — Bybit, OANDA, an MT5 bridge, TradingView, Polygon, Twelve
Data — means implementing one subclass and registering it. No analysis code
changes.
"""

from providers.base import (
    CANDLE_COLUMNS,
    TIMEFRAMES,
    MarketDataProvider,
    MarketDataUnavailableError,
    ProviderError,
    Quote,
    SymbolInfo,
    UnknownSymbolError,
    UnsupportedTimeframeError,
    normalize_symbol,
    normalize_timeframe,
    sort_timeframes,
    validate_candles,
)
from providers.registry import (
    UnknownProviderError,
    available_markets,
    default_provider_for,
    describe_providers,
    get_provider,
    register,
    resolve_name,
    shutdown,
)

__all__ = [
    'CANDLE_COLUMNS',
    'TIMEFRAMES',
    'MarketDataProvider',
    'MarketDataUnavailableError',
    'ProviderError',
    'Quote',
    'SymbolInfo',
    'UnknownProviderError',
    'UnknownSymbolError',
    'UnsupportedTimeframeError',
    'available_markets',
    'default_provider_for',
    'describe_providers',
    'get_provider',
    'normalize_symbol',
    'normalize_timeframe',
    'register',
    'resolve_name',
    'shutdown',
    'sort_timeframes',
    'validate_candles',
]
