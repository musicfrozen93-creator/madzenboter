"""Binance USDT-M Futures market data provider (crypto).

Translates between the platform symbol form ('BTCUSDT') and the ccxt unified
form ('BTC/USDT:USDT'), and adapts `exchange.client.ExchangeClient` to the
`MarketDataProvider` contract.

Public data only. The underlying client is constructed without credentials and
exposes no order, balance, or position method — see exchange/client.py.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from config.settings import Settings
from exchange.client import ExchangeClient
from providers.base import (
    MarketDataProvider,
    MarketDataUnavailableError,
    Quote,
    SymbolInfo,
    UnknownSymbolError,
    normalize_symbol,
    validate_candles,
)

logger = logging.getLogger(__name__)

# Binance USDT-M perpetuals settle in USDT; ccxt names them BASE/QUOTE:QUOTE.
_SETTLE = 'USDT'

# Timeframes Binance futures serves that are also in our canonical vocabulary.
_TIMEFRAMES: tuple[str, ...] = (
    '1m', '3m', '5m', '15m', '30m',
    '1h', '2h', '4h', '6h', '8h', '12h',
    '1d', '3d', '1w',
)


class BinanceProvider(MarketDataProvider):
    """Binance USDT-M Futures, exposed through the standardized provider API."""

    name = 'binance'
    market = 'crypto'
    timeframes = _TIMEFRAMES

    def __init__(self, settings: Settings, client: Optional[ExchangeClient] = None) -> None:
        """
        Args:
            settings: Application settings (non-credential config only).
            client: Optional pre-built market-data client, for tests.
        """
        self.settings = settings
        self._client = client or ExchangeClient(settings)
        self._symbol_map: dict[str, str] = {}   # 'BTCUSDT' -> 'BTC/USDT:USDT'

    # ── Lifecycle ──

    def initialize(self) -> None:
        """Load markets once and build the platform↔venue symbol map."""
        self._client.initialize()
        self._symbol_map = {
            self._to_platform(venue): venue
            for venue in self._client.get_all_futures_symbols()
        }
        logger.info(
            'BinanceProvider ready — %d USDT-M perpetuals', len(self._symbol_map)
        )

    # ── Symbol translation ──

    @staticmethod
    def _to_platform(venue_symbol: str) -> str:
        """'BTC/USDT:USDT' -> 'BTCUSDT'."""
        pair = venue_symbol.split(':', 1)[0]
        return pair.replace('/', '').upper()

    def _to_venue(self, symbol: str) -> str:
        """'BTCUSDT' -> 'BTC/USDT:USDT'.

        Raises:
            UnknownSymbolError: If the symbol is malformed or not listed.
        """
        canonical = normalize_symbol(symbol)
        venue = self._symbol_map.get(canonical)
        if venue:
            return venue
        # Markets not loaded yet (e.g. a unit test constructed the provider
        # directly): fall back to deriving the ccxt form for USDT-quoted pairs.
        if not self._symbol_map and canonical.endswith(_SETTLE):
            base = canonical[: -len(_SETTLE)]
            if base:
                return f'{base}/{_SETTLE}:{_SETTLE}'
        raise UnknownSymbolError(f'{canonical} is not a Binance USDT-M perpetual')

    # ── Capability discovery ──

    def list_symbols(self) -> list[str]:
        return sorted(self._symbol_map)

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        venue = self._to_venue(symbol)
        canonical = self._to_platform(venue)
        try:
            market = self._client.get_symbol_info(venue)
        except Exception as exc:
            raise UnknownSymbolError(f'No market metadata for {canonical}: {exc}') from exc

        # ccxt reports price precision either as a tick size (0.01) or as a
        # decimal-place count (2), depending on the venue.
        precision = (market.get('precision') or {}).get('price', 8)
        tick_size: Optional[float] = None
        if isinstance(precision, float) and precision > 0:
            tick_size = precision
            text = f'{precision:.10f}'.rstrip('0')
            decimals = len(text.split('.', 1)[1]) if '.' in text else 0
        else:
            decimals = int(precision)

        return SymbolInfo(
            symbol=canonical,
            base=str(market.get('base') or canonical[: -len(_SETTLE)]),
            quote=str(market.get('quote') or _SETTLE),
            price_precision=decimals,
            tick_size=tick_size,
            active=bool(market.get('active', True)),
        )

    # ── Market data ──

    def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 300
    ) -> pd.DataFrame:
        venue = self._to_venue(symbol)
        interval = self.ensure_timeframe(timeframe)
        try:
            df = self._client.fetch_ohlcv(venue, interval, limit=limit)
        except Exception as exc:
            raise MarketDataUnavailableError(
                f'Binance candle fetch failed for {symbol} {interval}: {exc}'
            ) from exc
        return validate_candles(df, symbol)

    def fetch_quote(self, symbol: str) -> Quote:
        venue = self._to_venue(symbol)
        try:
            ticker = self._client.fetch_ticker(venue)
        except Exception as exc:
            raise MarketDataUnavailableError(
                f'Binance ticker fetch failed for {symbol}: {exc}'
            ) from exc
        return Quote(
            symbol=normalize_symbol(symbol),
            last=float(ticker.get('last') or 0.0),
            bid=float(ticker.get('bid') or 0.0),
            ask=float(ticker.get('ask') or 0.0),
            spread=float(ticker.get('spread') or 0.0),
            quote_volume_24h=float(ticker.get('quoteVolume') or 0.0),
        )

    # ── Venue extras ──

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        try:
            return self._client.fetch_funding_rate(self._to_venue(symbol))
        except Exception as exc:
            logger.debug('Funding rate unavailable for %s: %s', symbol, exc)
            return None

    def reference_symbol(self) -> Optional[str]:
        """BTC is the systemic benchmark for crypto."""
        return 'BTCUSDT'
