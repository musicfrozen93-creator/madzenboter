"""
Zentry Futures Core — CCXT Exchange Client.

Wraps ccxt.binance for USDT-M Futures operations: market data,
account management, and order execution. Includes automatic retry
with exponential backoff and comprehensive error handling.
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
    """CCXT-based Binance USDT-M Futures client.

    Handles all exchange communication with built-in rate limiting,
    automatic retries on transient errors, and precision validation.
    """

    def __init__(self, settings: Settings, api_key: str = '', api_secret: str = '') -> None:
        """Initialise the exchange client.

        Args:
            settings: Application settings containing API credentials.
            api_key: Optional per-account API key (overrides settings).
            api_secret: Optional per-account API secret (overrides settings).
        """
        self.settings = settings
        self._api_key = api_key
        self._api_secret = api_secret
        self.exchange: Optional[ccxt.binance] = None
        self.markets: dict = {}

    @classmethod
    def for_account(cls, settings: Settings, api_key: str, api_secret: str) -> 'ExchangeClient':
        """Create an ExchangeClient bound to specific API credentials.

        Args:
            settings: Application settings (used for non-credential config).
            api_key: Account-specific API key.
            api_secret: Account-specific API secret.

        Returns:
            A new ExchangeClient configured with the given credentials.
        """
        return cls(settings, api_key=api_key, api_secret=api_secret)

    # ───────────────────────────────────────────
    # Initialisation
    # ───────────────────────────────────────────

    def initialize(self) -> None:
        """Create the CCXT exchange instance and load markets.

        Sets sandbox mode if use_testnet is True.
        Loads all market information for symbol lookups.
        """
        self.exchange = ccxt.binance({
            'apiKey': self._api_key or self.settings.api_key,
            'secret': self._api_secret or self.settings.api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
            },
        })

        if self.settings.use_testnet:
            self.exchange.set_sandbox_mode(True)
            logger.info('Exchange initialised in TESTNET mode')
        else:
            logger.info('Exchange initialised in LIVE mode')

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

    # ───────────────────────────────────────────
    # Account
    # ───────────────────────────────────────────

    def fetch_balance(self) -> dict:
        """Fetch USDT futures wallet balance.

        Returns:
            Dict with keys: total, free, used (all floats in USDT).
        """
        balance = self._retry(lambda: self.exchange.fetch_balance())
        usdt = balance.get('USDT', balance.get('total', {}))
        if isinstance(usdt, dict):
            return {
                'total': float(usdt.get('total', 0) or 0),
                'free': float(usdt.get('free', 0) or 0),
                'used': float(usdt.get('used', 0) or 0),
            }
        # Fallback
        return {
            'total': float(balance.get('total', {}).get('USDT', 0) or 0),
            'free': float(balance.get('free', {}).get('USDT', 0) or 0),
            'used': float(balance.get('used', {}).get('USDT', 0) or 0),
        }

    def fetch_positions(self) -> List[dict]:
        """Fetch all open futures positions.

        Returns:
            List of position dicts with standardised keys.
        """
        positions = self._retry(lambda: self.exchange.fetch_positions())
        open_positions = []
        for pos in positions:
            contracts = float(pos.get('contracts', 0) or 0)
            if contracts > 0:
                open_positions.append({
                    'symbol': pos.get('symbol', ''),
                    'side': pos.get('side', ''),
                    'contracts': contracts,
                    'entryPrice': float(pos.get('entryPrice', 0) or 0),
                    'unrealizedPnl': float(pos.get('unrealizedPnl', 0) or 0),
                    'leverage': int(pos.get('leverage', 1) or 1),
                    'liquidationPrice': float(pos.get('liquidationPrice', 0) or 0),
                })
        return open_positions

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
    # Account Configuration
    # ───────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a symbol. Silently handles 'no change needed'.

        Args:
            symbol: Trading pair.
            leverage: Desired leverage (e.g. 5, 8, 10).
        """
        try:
            self.exchange.set_leverage(leverage, symbol)
            logger.debug('Leverage set to %dx for %s', leverage, symbol)
        except Exception as e:
            msg = str(e).lower()
            if 'no need to change' in msg or 'leverage not modified' in msg:
                logger.debug('Leverage already %dx for %s', leverage, symbol)
            else:
                logger.warning('Failed to set leverage for %s: %s', symbol, e)

    def set_margin_mode(self, symbol: str, mode: str = 'cross') -> None:
        """Set margin mode for a symbol. Silently handles 'no change needed'.

        Args:
            symbol: Trading pair.
            mode: 'cross' or 'isolated'.
        """
        try:
            self.exchange.set_margin_mode(mode, symbol)
            logger.debug('Margin mode set to %s for %s', mode, symbol)
        except Exception as e:
            msg = str(e).lower()
            if 'no need to change' in msg or 'margin type' in msg:
                logger.debug('Margin mode already %s for %s', mode, symbol)
            else:
                logger.warning('Failed to set margin mode for %s: %s', symbol, e)

    # ───────────────────────────────────────────
    # Order Execution
    # ───────────────────────────────────────────

    def place_market_order(
        self, symbol: str, side: str, quantity: float, reduce_only: bool = False
    ) -> dict:
        """Place a market order on Binance Futures.

        Args:
            symbol: Trading pair.
            side: 'buy' or 'sell'.
            quantity: Order quantity in base currency.
            reduce_only: If True, order can only reduce a position.

        Returns:
            CCXT order response dict.
        """
        params: dict[str, Any] = {}
        if reduce_only:
            params['reduceOnly'] = True

        # Precision
        quantity = float(self.exchange.amount_to_precision(symbol, quantity))

        logger.info(
            'Placing %s market %s %.8f %s (reduceOnly=%s)',
            symbol, side, quantity, symbol, reduce_only
        )

        order = self._retry(
            lambda: self.exchange.create_order(
                symbol=symbol, type='market', side=side,
                amount=quantity, params=params,
            )
        )
        logger.info(
            'Order filled: %s %s %.8f @ %.4f | ID: %s',
            side, symbol, quantity,
            float(order.get('average', order.get('price', 0)) or 0),
            order.get('id', 'N/A'),
        )
        return order

    def close_position(self, symbol: str, side: str, quantity: float) -> dict:
        """Close a position by placing a counter market order.

        Args:
            symbol: Trading pair.
            side: Position side ('long' or 'short').
            quantity: Quantity to close.

        Returns:
            CCXT order response dict.
        """
        close_side = 'sell' if side == 'long' else 'buy'
        return self.place_market_order(symbol, close_side, quantity, reduce_only=True)

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
