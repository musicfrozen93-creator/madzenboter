"""
Zentry — CCXT Market Data Client.

Wraps ccxt.binance for READ-ONLY USDT-M Futures market data: OHLCV candles,
tickers, funding rates, and symbol metadata. Includes automatic retry with
exponential backoff and comprehensive error handling.

This client is keyless by construction. It cannot read a balance, read a
position, change leverage, or place an order — those capabilities existed only
to trade automatically and were removed in the Phase 0 conversion to a manual
AI signal platform.
"""

import logging
import time
from typing import Any, List, Optional

import ccxt
import pandas as pd

from config.settings import Settings

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_BACKOFF = 1.0  # seconds


class ExchangeClient:
    """CCXT-based Binance USDT-M Futures market-data client.

    Handles all exchange communication with built-in rate limiting and
    automatic retries on transient errors. It is public/read-only: no API
    credentials are ever supplied, so the exchange rejects any private call.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialise the market-data client.

        Args:
            settings: Application settings (non-credential config only).
        """
        self.settings = settings
        self.exchange: Optional[ccxt.binance] = None
        self.markets: dict = {}

    # ───────────────────────────────────────────
    # Initialisation
    # ───────────────────────────────────────────

    def initialize(self) -> None:
        """Create the CCXT exchange instance and load markets.

        Sets sandbox mode if use_testnet is True.
        Loads all market information for symbol lookups.
        """
        # No apiKey/secret is passed: this client is public by construction and
        # is structurally unable to reach any private (account/order) endpoint.
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
            },
        })

        mode = 'TESTNET' if self.settings.use_testnet else 'LIVE'
        if self.settings.use_testnet:
            self.exchange.set_sandbox_mode(True)
        logger.info('Exchange initialised in %s mode (public/market-data-only)', mode)

        self.markets = self.exchange.load_markets()
        logger.info('Loaded %d markets', len(self.markets))

    # ───────────────────────────────────────────
    # Market Data
    # ───────────────────────────────────────────

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> pd.DataFrame:
        """Fetch OHLCV candlestick data.

        Args:
            symbol: Trading pair (e.g. 'BTC/USDT:USDT').
            timeframe: Candle interval ('1m', '5m', '15m', '1h', etc.).
            limit: Maximum number of candles to fetch (max 1500).

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume.
        """
        data = self._retry(
            lambda: self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        )
        if not data:
            return pd.DataFrame(
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
        df = pd.DataFrame(
            data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df

    def fetch_ticker(self, symbol: str) -> dict:
        """Fetch the latest ticker for a symbol.

        Args:
            symbol: Trading pair.

        Returns:
            Dict with keys: last, bid, ask, spread.
        """
        ticker = self._retry(lambda: self.exchange.fetch_ticker(symbol))
        bid = ticker.get('bid') or ticker.get('last', 0)
        ask = ticker.get('ask') or ticker.get('last', 0)
        return {
            'last': ticker.get('last', 0),
            'bid': bid,
            'ask': ask,
            'spread': ask - bid if ask and bid else 0,
            'quoteVolume': ticker.get('quoteVolume', 0),
        }

    def fetch_all_tickers(self) -> dict:
        """Fetch tickers for all USDT-M futures pairs.

        Returns:
            Dict keyed by symbol with ticker dicts as values.
        """
        raw_tickers = self._retry(
            lambda: self.exchange.fetch_tickers(params={'type': 'future'})
        )
        result = {}
        for sym, t in raw_tickers.items():
            if ':USDT' in sym:
                bid = t.get('bid') or t.get('last', 0)
                ask = t.get('ask') or t.get('last', 0)
                result[sym] = {
                    'last': t.get('last', 0),
                    'bid': bid,
                    'ask': ask,
                    'spread': ask - bid if ask and bid else 0,
                    'quoteVolume': t.get('quoteVolume', 0),
                }
        return result

    def fetch_funding_rate(self, symbol: str) -> float:
        """Fetch current funding rate for a symbol.

        Args:
            symbol: Trading pair.

        Returns:
            Funding rate as a float (e.g. 0.0001 = 0.01%).
        """
        try:
            funding = self._retry(
                lambda: self.exchange.fetch_funding_rate(symbol)
            )
            return float(funding.get('fundingRate', 0) or 0)
        except Exception as e:
            logger.debug('Failed to fetch funding rate for %s: %s', symbol, e)
            return 0.0

    # ───────────────────────────────────────────
    # Symbol Info
    # ───────────────────────────────────────────

    def get_symbol_info(self, symbol: str) -> dict:
        """Get market information for a symbol.

        Args:
            symbol: Trading pair.

        Returns:
            CCXT market dict with precision, limits, etc.
        """
        return self.exchange.market(symbol)

    def get_all_futures_symbols(self) -> List[str]:
        """Get all active USDT-M perpetual futures symbols.

        Returns:
            List of symbol strings (e.g. ['BTC/USDT:USDT', ...]).
        """
        symbols = []
        for sym, market in self.markets.items():
            if (
                market.get('active', False)
                and market.get('settle') == 'USDT'
                and market.get('type') == 'swap'
                and market.get('linear', False)
            ):
                symbols.append(sym)
        return symbols

    # ───────────────────────────────────────────
    # Retry Logic
    # ───────────────────────────────────────────

    def _retry(self, operation: callable, max_retries: int = MAX_RETRIES) -> Any:
        """Execute an operation with exponential backoff on transient errors.

        Args:
            operation: Callable to execute (lambda wrapping exchange call).
            max_retries: Maximum number of retry attempts.

        Returns:
            Result of the operation.

        Raises:
            The last exception if all retries are exhausted.
        """
        last_error = None
        for attempt in range(max_retries):
            try:
                return operation()
            except (
                ccxt.NetworkError,
                ccxt.ExchangeNotAvailable,
                ccxt.RequestTimeout,
            ) as e:
                last_error = e
                wait = BASE_BACKOFF * (2 ** attempt)
                logger.warning(
                    'Transient error (attempt %d/%d): %s — retrying in %.1fs',
                    attempt + 1, max_retries, e, wait
                )
                time.sleep(wait)
            except ccxt.RateLimitExceeded as e:
                last_error = e
                wait = BASE_BACKOFF * (2 ** (attempt + 1))
                logger.warning('Rate limit hit, waiting %.1fs', wait)
                time.sleep(wait)
            except Exception:
                raise
        raise last_error
