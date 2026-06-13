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
        # Cached account position mode: None=unknown, True=hedge (dual-side),
        # False=one-way. Determined lazily on first order and reused.
        self._hedge_mode: Optional[bool] = None

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

    @classmethod
    def for_market_data(cls, settings: Settings) -> 'ExchangeClient':
        """Create a PUBLIC, keyless client for market data only.

        Used by the scanner and signal engine. It carries no API credentials
        and is structurally unable to place orders or read balances — those
        require per-account clients created via ``for_account``.

        Args:
            settings: Application settings (non-credential config only).

        Returns:
            A new keyless ExchangeClient for public market data.
        """
        return cls(settings, api_key='', api_secret='')

    @property
    def has_credentials(self) -> bool:
        """True if this client carries trading credentials."""
        return bool(self._api_key and self._api_secret)

    # ───────────────────────────────────────────
    # Initialisation
    # ───────────────────────────────────────────

    def initialize(self) -> None:
        """Create the CCXT exchange instance and load markets.

        Sets sandbox mode if use_testnet is True.
        Loads all market information for symbol lookups.
        """
        # Credentials come ONLY from the per-account keys passed to this client.
        # There is no fall-back to settings/master/env keys: a client created
        # without keys (for_market_data) is public/read-only by construction.
        self.exchange = ccxt.binance({
            'apiKey': self._api_key,
            'secret': self._api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
            },
        })

        mode = 'TESTNET' if self.settings.use_testnet else 'LIVE'
        if self.settings.use_testnet:
            self.exchange.set_sandbox_mode(True)
        access = 'authenticated' if self.has_credentials else 'public/market-data-only'
        logger.info('Exchange initialised in %s mode (%s)', mode, access)

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

    def get_position_mode(self) -> bool:
        """Return True if the account is in Hedge (dual-side) mode, else one-way.

        Binance rejects close orders that don't match the account's position
        mode: one-way uses ``reduceOnly``; hedge mode requires ``positionSide``
        (LONG/SHORT) and does NOT accept ``reduceOnly``. The result is cached;
        on any error we assume one-way (the historical behaviour) so detection
        failure never blocks trading.

        Returns:
            True for hedge mode, False for one-way.
        """
        if self._hedge_mode is not None:
            return self._hedge_mode
        try:
            res = self.exchange.fapiPrivateGetPositionSideDual()
            dual = res.get('dualSidePosition')
            self._hedge_mode = dual is True or str(dual).lower() == 'true'
            logger.info(
                'Account position mode detected: %s',
                'HEDGE (dual-side)' if self._hedge_mode else 'ONE-WAY',
            )
        except Exception as e:
            logger.warning(
                'Could not determine position mode (%s) — assuming ONE-WAY', e
            )
            self._hedge_mode = False
        return self._hedge_mode

    def fetch_position(self, symbol: str) -> dict:
        """Fetch the LIVE position for a single symbol from the exchange.

        Args:
            symbol: Trading pair.

        Returns:
            Dict: {'contracts': float (>=0 magnitude), 'side': 'long'/'short'/None,
                   'entryPrice': float, 'unrealizedPnl': float}. contracts == 0
            and side is None when the symbol is flat on the exchange.
        """
        try:
            positions = self._retry(lambda: self.exchange.fetch_positions([symbol]))
        except Exception:
            # Some ccxt/exchange combos reject the per-symbol filter — fall back.
            positions = self._retry(lambda: self.exchange.fetch_positions())

        for pos in positions:
            if pos.get('symbol') != symbol:
                continue
            contracts = abs(float(pos.get('contracts', 0) or 0))
            if contracts <= 0:
                continue
            side = pos.get('side')  # ccxt: 'long' / 'short'
            if side not in ('long', 'short'):
                # Derive from signed contracts if ccxt didn't populate side.
                signed = float(pos.get('contracts', 0) or 0)
                side = 'long' if signed >= 0 else 'short'
            return {
                'contracts': contracts,
                'side': side,
                'entryPrice': float(pos.get('entryPrice', 0) or 0),
                'unrealizedPnl': float(pos.get('unrealizedPnl', 0) or 0),
            }
        return {'contracts': 0.0, 'side': None, 'entryPrice': 0.0, 'unrealizedPnl': 0.0}

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
        # Hard guard: a keyless (market-data) client must never place orders.
        if not self.has_credentials:
            raise PermissionError(
                'place_market_order called on a keyless market-data client — '
                'orders are only allowed on per-account clients.'
            )

        params: dict[str, Any] = {}
        hedge = self.get_position_mode()
        if hedge:
            # Hedge mode: every order MUST carry positionSide and must NOT carry
            # reduceOnly. For an opening/adding order, buy→LONG, sell→SHORT.
            params['positionSide'] = 'LONG' if side == 'buy' else 'SHORT'
        elif reduce_only:
            params['reduceOnly'] = True

        # Precision
        quantity = float(self.exchange.amount_to_precision(symbol, quantity))

        logger.info(
            'Placing %s market %s %.8f %s (reduceOnly=%s hedge_mode=%s params=%s)',
            symbol, side, quantity, symbol,
            reduce_only and not hedge, hedge, params,
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
        """Close (or reduce) a position, reconciled against the LIVE exchange state.

        Root-cause fix for Binance -2022 "ReduceOnly Order is rejected": instead
        of blindly closing the DB-tracked quantity/side with reduceOnly (which
        Binance rejects whenever the live position is already flat, smaller, or
        on the opposite side), this:

          1. queries the live position,
          2. logs full diagnostics (live size/side, close side, reduceOnly flag,
             hedge-mode status),
          3. returns ``{'already_flat': True}`` WITHOUT ordering if nothing is
             open (so the caller reconciles the DB instead of failing),
          4. derives the close side from the LIVE position side,
          5. clamps the close quantity to what actually exists (never oversize),
          6. uses ``positionSide`` in hedge mode and ``reduceOnly`` in one-way.

        Args:
            symbol: Trading pair.
            side: DB-tracked position side ('long' or 'short').
            quantity: Desired quantity to close (clamped to the live size).

        Returns:
            CCXT order dict on a real close, or
            ``{'already_flat': True, 'symbol': symbol, 'filled': 0.0}`` when the
            exchange shows no position to reduce.
        """
        if not self.has_credentials:
            raise PermissionError(
                'close_position called on a keyless market-data client.'
            )

        hedge = self.get_position_mode()
        live = self.fetch_position(symbol)
        live_qty = live['contracts']
        live_side = live['side']
        intended_close_side = 'sell' if side == 'long' else 'buy'

        # ── Diagnostics requested for the -2022 investigation ──
        logger.info(
            'CLOSE_DIAGNOSTIC %s | db_side=%s db_req_qty=%.8f | '
            'exch_pos_size=%.8f exch_pos_side=%s | close_side=%s '
            'reduceOnly=%s hedge_mode=%s',
            symbol, side, quantity, live_qty, live_side,
            intended_close_side, (not hedge), hedge,
        )

        # ── (5) Position already closed on exchange → reconcile, don't error ──
        if live_qty <= 0 or live_side is None:
            logger.warning(
                'CLOSE_SKIP %s | exchange shows NO open position (already flat) — '
                'reduceOnly would be rejected (-2022). Reconciling DB; no order sent.',
                symbol,
            )
            return {'already_flat': True, 'symbol': symbol, 'filled': 0.0}

        # ── (3) Derive close side from the LIVE position, not stale DB state ──
        close_side = 'sell' if live_side == 'long' else 'buy'
        if live_side != side:
            logger.warning(
                'CLOSE_SIDE_MISMATCH %s | db_side=%s but exchange_side=%s — '
                'closing the EXCHANGE side (%s).',
                symbol, side, live_side, close_side,
            )

        # ── (4) Clamp to the real position size; never try to over-reduce ──
        close_qty = min(quantity, live_qty) if quantity > 0 else live_qty
        if quantity > live_qty:
            logger.warning(
                'CLOSE_SIZE_MISMATCH %s | db_qty=%.8f > exch_qty=%.8f — '
                'closing only the live %.8f to avoid -2022.',
                symbol, quantity, live_qty, live_qty,
            )
        close_qty = float(self.exchange.amount_to_precision(symbol, close_qty))
        if close_qty <= 0:
            logger.warning(
                'CLOSE_SKIP %s | close qty rounds to 0 (live=%.8f) — reconciling.',
                symbol, live_qty,
            )
            return {'already_flat': True, 'symbol': symbol, 'filled': 0.0}

        # ── (1/2) Mode-correct params: hedge→positionSide, one-way→reduceOnly ──
        params: dict[str, Any] = {}
        if hedge:
            params['positionSide'] = 'LONG' if live_side == 'long' else 'SHORT'
        else:
            params['reduceOnly'] = True

        logger.info(
            'Closing %s | side=%s qty=%.8f hedge_mode=%s params=%s',
            symbol, close_side, close_qty, hedge, params,
        )
        order = self._retry(
            lambda: self.exchange.create_order(
                symbol=symbol, type='market', side=close_side,
                amount=close_qty, params=params,
            )
        )
        logger.info(
            'Close filled: %s %s %.8f @ %.4f | ID: %s',
            close_side, symbol, close_qty,
            float(order.get('average', order.get('price', 0)) or 0),
            order.get('id', 'N/A'),
        )
        return order

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
