"""Twelve Data forex market-data provider.

Adds FOREX to the platform through the existing `MarketDataProvider` abstraction
— the Analysis Engine is unchanged and never learns this provider exists beyond
the standardized candles/quotes it returns.

This provider is READ-ONLY market data. It is registered ONLY when
`TWELVEDATA_API_KEY` is set (see `providers.registry`), so a deployment without a
forex data key simply does not offer the forex market rather than failing.

Uses the Python standard library (`urllib`) — no new dependency.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Optional

import pandas as pd

from config.settings import Settings
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

_BASE_URL = 'https://api.twelvedata.com/time_series'
_TIMEOUT = 15  # seconds

# Canonical timeframe → Twelve Data interval. Covers every rung the
# multi-timeframe ladder uses for the selectable forex timeframes.
_INTERVALS = {
    '15m': '15min', '30m': '30min', '1h': '1h', '2h': '2h',
    '4h': '4h', '1d': '1day', '1w': '1week',
}

# A curated set of liquid FX pairs + metals for the symbol selector. Users may
# still analyse any pair Twelve Data serves; this only seeds the dropdown.
_SYMBOLS = [
    'EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'AUDUSD', 'USDCAD', 'NZDUSD',
    'EURGBP', 'EURJPY', 'GBPJPY', 'EURCHF', 'AUDJPY', 'EURAUD', 'GBPAUD',
    'USDSGD', 'USDHKD', 'USDMXN', 'USDZAR', 'USDTRY',
    'XAUUSD', 'XAGUSD',  # gold, silver
]


class TwelveDataForexProvider(MarketDataProvider):
    """Forex OHLCV via Twelve Data, exposed through the standard provider API."""

    name = 'twelvedata'
    market = 'forex'
    timeframes = ('15m', '30m', '1h', '2h', '4h', '1d', '1w')

    def __init__(self, settings: Settings, api_key: Optional[str] = None) -> None:
        self.settings = settings
        self._api_key = api_key or os.environ.get('TWELVEDATA_API_KEY', '')
        self._symbols = list(_SYMBOLS)

    # ── Lifecycle ──

    def initialize(self) -> None:
        if not self._api_key:
            # Should not happen (registry gates on the key), but fail loudly.
            raise MarketDataUnavailableError(
                'TWELVEDATA_API_KEY is not configured; forex data is unavailable.'
            )
        logger.info('TwelveDataForexProvider ready — %d curated pairs', len(self._symbols))

    # ── Symbol translation ──

    @staticmethod
    def _to_pair(symbol: str) -> str:
        """'EURUSD' or 'EUR_USD' → 'EUR/USD' (Twelve Data form)."""
        canonical = normalize_symbol(symbol)
        if '_' in canonical:
            base, quote = canonical.split('_', 1)
        elif len(canonical) == 6:
            base, quote = canonical[:3], canonical[3:]
        else:
            raise UnknownSymbolError(f'{canonical} is not a recognisable forex pair')
        return f'{base}/{quote}'

    # ── Capability discovery ──

    def list_symbols(self) -> list[str]:
        return sorted(self._symbols)

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        canonical = normalize_symbol(symbol)
        pair = self._to_pair(canonical)
        base, quote = pair.split('/', 1)
        # FX convention: JPY quotes ~3 dp, metals ~2 dp, everything else ~5 dp.
        if quote == 'JPY':
            precision = 3
        elif base in ('XAU', 'XAG'):
            precision = 2
        else:
            precision = 5
        return SymbolInfo(
            symbol=canonical, base=base, quote=quote,
            price_precision=precision, active=True,
        )

    # ── Market data ──

    def _request(self, symbol: str, interval: str, limit: int) -> dict:
        params = urllib.parse.urlencode({
            'symbol': self._to_pair(symbol),
            'interval': interval,
            'outputsize': min(max(limit, 1), 5000),
            'apikey': self._api_key,
            'format': 'JSON',
            'timezone': 'UTC',
        })
        url = f'{_BASE_URL}?{params}'
        try:
            with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
        except Exception as exc:
            raise MarketDataUnavailableError(
                f'Twelve Data request failed for {symbol} {interval}: {exc}'
            ) from exc

        # Twelve Data signals errors with status='error' + a message.
        if isinstance(payload, dict) and payload.get('status') == 'error':
            message = payload.get('message', 'unknown error')
            # A bad symbol vs. a service problem produce different user errors.
            if 'symbol' in message.lower():
                raise UnknownSymbolError(f'{symbol}: {message}')
            raise MarketDataUnavailableError(f'Twelve Data error for {symbol}: {message}')
        return payload

    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        interval = self.ensure_timeframe(timeframe)
        td_interval = _INTERVALS[interval]
        payload = self._request(symbol, td_interval, limit)

        values = payload.get('values') if isinstance(payload, dict) else None
        if not values:
            raise MarketDataUnavailableError(f'No candles returned for {symbol} {interval}')

        # Twelve Data returns newest-first; reverse to oldest-first.
        rows = list(reversed(values))
        df = pd.DataFrame({
            'timestamp': pd.to_datetime([r.get('datetime') for r in rows], utc=False, errors='coerce'),
            'open': [r.get('open') for r in rows],
            'high': [r.get('high') for r in rows],
            'low': [r.get('low') for r in rows],
            'close': [r.get('close') for r in rows],
            # Forex has no consolidated volume; default to 0 when absent.
            'volume': [r.get('volume', 0) or 0 for r in rows],
        })
        return validate_candles(df, symbol)

    def fetch_quote(self, symbol: str) -> Quote:
        # Derive the quote from the latest 1-minute-ish candle to avoid a second
        # billed endpoint. Forex bid/ask spread is not exposed on this feed, so
        # spread is reported as 0 (unknown) rather than fabricated.
        df = self.fetch_candles(symbol, '15m', limit=2)
        last = float(df['close'].iloc[-1])
        return Quote(symbol=normalize_symbol(symbol), last=last, bid=last, ask=last, spread=0.0)

    def reference_symbol(self) -> Optional[str]:
        # Forex has no single BTC-style systemic benchmark; the engine handles a
        # missing benchmark by skipping the systemic-risk gate.
        return None
