"""
Zentry Futures Core — Signal Engine.

Generates entry signals using RSI + EMA200 and classifies market
regime using ADX (for regime only, never for entry decisions).
"""

import logging
import time
from typing import Optional, Tuple

import pandas as pd

from config.settings import BtcRegime, MarketRegime, Settings, VolatilityLevel
from core.dto import Signal
from exchange.client import ExchangeClient
from signals.btc_regime import classify_btc_regime, regime_allows_side
from signals.indicators import compute_adx, compute_atr, compute_ema, compute_rsi

logger = logging.getLogger(__name__)


class SignalEngine:
    """Generates entry signals and classifies market conditions.

    Entry Logic (strict RSI + EMA200 only):
      LONG:  price > EMA200 AND RSI < 30
      SHORT: price < EMA200 AND RSI > 70

    Market Classification (ADX + ATR):
      Regime:     ADX > 25 → TRENDING, ADX < 20 → SIDEWAYS
      Volatility: ATR vs 30-bar avg ATR → LOW / MEDIUM / HIGH
    """

    def __init__(self, exchange_client: ExchangeClient, settings: Settings) -> None:
        """Initialise the signal engine.

        Args:
            exchange_client: Exchange client for fetching OHLCV data.
            settings: Application settings.
        """
        self.exchange = exchange_client
        self.settings = settings
        # Cached BTC regime (recomputed at most every btc_regime_cache_seconds).
        self._btc_regime: BtcRegime = BtcRegime.UNKNOWN
        self._btc_regime_ts: float = 0.0

    def get_btc_regime(self) -> BtcRegime:
        """Return the current BTC regime, cached for btc_regime_cache_seconds.

        Fetches BTC 1h candles and classifies the regime. On any failure it
        fails safe by returning UNKNOWN (which allows trading in both
        directions). The result is cached to avoid refetching BTC data for
        every symbol evaluated within a loop.
        """
        now = time.time()
        ttl = max(0, self.settings.btc_regime_cache_seconds)
        if self._btc_regime_ts and (now - self._btc_regime_ts) < ttl:
            return self._btc_regime

        try:
            df_btc = self.exchange.fetch_ohlcv(
                self.settings.btc_symbol, self.settings.trend_timeframe, limit=250
            )
            regime = classify_btc_regime(df_btc, self.settings)
        except Exception as e:
            logger.warning(
                'BTC regime fetch failed for %s: %s — failing safe (UNKNOWN, '
                'trading allowed both directions)', self.settings.btc_symbol, e,
            )
            regime = BtcRegime.UNKNOWN

        self._btc_regime = regime
        self._btc_regime_ts = now
        logger.info('BTC_REGIME | %s (cached %ds)', regime.value.upper(), ttl)
        return regime

    def classify_market(
        self, df_1h: pd.DataFrame, df_5m: pd.DataFrame
    ) -> Tuple[MarketRegime, VolatilityLevel]:
        """Classify current market regime and volatility.

        ADX is used ONLY here for regime detection — never for entries.

        Args:
            df_1h: 1-hour OHLCV DataFrame.
            df_5m: 5-minute OHLCV DataFrame.

        Returns:
            Tuple of (MarketRegime, VolatilityLevel).
        """
        # ── Regime via ADX on 1h ──
        regime = MarketRegime.UNKNOWN
        if len(df_1h) >= self.settings.adx_period * 2:
            adx = compute_adx(
                df_1h['high'], df_1h['low'], df_1h['close'],
                period=self.settings.adx_period,
            )
            latest_adx = adx.dropna().iloc[-1] if not adx.dropna().empty else 0
            if latest_adx > self.settings.adx_trend_threshold:
                regime = MarketRegime.TRENDING
            elif latest_adx < self.settings.adx_sideways_threshold:
                regime = MarketRegime.SIDEWAYS

        # ── Volatility via ATR on 5m ──
        volatility = VolatilityLevel.MEDIUM
        if len(df_5m) >= self.settings.atr_period + 30:
            atr = compute_atr(
                df_5m['high'], df_5m['low'], df_5m['close'],
                period=self.settings.atr_period,
            )
            atr_clean = atr.dropna()
            if len(atr_clean) >= 30:
                current_atr = atr_clean.iloc[-1]
                avg_atr = atr_clean.iloc[-30:].mean()
                if avg_atr > 0:
                    ratio = current_atr / avg_atr
                    if ratio > self.settings.high_vol_atr_multiplier:
                        volatility = VolatilityLevel.HIGH
                    elif ratio < self.settings.low_vol_atr_multiplier:
                        volatility = VolatilityLevel.LOW

        return regime, volatility

    def generate_signal(self, symbol: str) -> Optional[Signal]:
        """Generate an entry signal for a symbol.

        Uses 1h candles for EMA200 trend filter and 5m candles for RSI
        signal generation. ADX is NOT used for entry decisions.

        Args:
            symbol: Trading pair (e.g. 'BTC/USDT:USDT').

        Returns:
            Signal if entry conditions are met, None otherwise.
        """
        try:
            # Fetch candle data
            df_1h = self.exchange.fetch_ohlcv(
                symbol, self.settings.trend_timeframe, limit=250
            )
            if len(df_1h) < self.settings.ema_period:
                logger.info(
                    'SIGNAL_REJECTED %s | stage=data | insufficient 1h data '
                    '(%d bars, need %d for EMA%d)',
                    symbol, len(df_1h), self.settings.ema_period, self.settings.ema_period,
                )
                return None

            df_5m = self.exchange.fetch_ohlcv(
                symbol, self.settings.signal_timeframe, limit=100
            )
            if len(df_5m) < self.settings.rsi_period + 5:
                logger.info(
                    'SIGNAL_REJECTED %s | stage=data | insufficient 5m data (%d bars)',
                    symbol, len(df_5m),
                )
                return None

            # ── Compute indicators ──
            ema200 = compute_ema(df_1h['close'], period=self.settings.ema_period)
            rsi = compute_rsi(df_5m['close'], period=self.settings.rsi_period)
            atr = compute_atr(
                df_5m['high'], df_5m['low'], df_5m['close'],
                period=self.settings.atr_period,
            )

            latest_ema = ema200.dropna().iloc[-1] if not ema200.dropna().empty else None
            latest_rsi = rsi.dropna().iloc[-1] if not rsi.dropna().empty else None
            latest_atr = atr.dropna().iloc[-1] if not atr.dropna().empty else None
            current_price = float(df_5m['close'].iloc[-1])

            if latest_ema is None or latest_rsi is None or latest_atr is None:
                return None

            if pd.isna(latest_ema) or pd.isna(latest_rsi) or pd.isna(latest_atr):
                return None

            latest_ema = float(latest_ema)
            latest_rsi = float(latest_rsi)
            latest_atr = float(latest_atr)

            # ── Classify market ──
            regime, volatility = self.classify_market(df_1h, df_5m)

            # ── Entry conditions (configurable RSI thresholds + optional EMA200 trend filter) ──
            # LONG  = (price > EMA200 if trend filter on) AND RSI < rsi_long_threshold
            # SHORT = (price < EMA200 if trend filter on) AND RSI > rsi_short_threshold
            # Thresholds come from config (defaults 40/60); the EMA filter is
            # mandatory by default and protects the averaging grid from trends.
            long_thr = self.settings.rsi_long_threshold
            short_thr = self.settings.rsi_short_threshold
            require_ema = self.settings.require_ema_trend_filter

            ema_ok_long = (not require_ema) or (current_price > latest_ema)
            ema_ok_short = (not require_ema) or (current_price < latest_ema)
            ema_status = (
                'above' if current_price > latest_ema
                else 'below' if current_price < latest_ema else 'at'
            ) + ' EMA200'

            side: Optional[str] = None
            strength: float = 0.0

            if ema_ok_long and latest_rsi < long_thr:
                # LONG: (uptrend) RSI pulled back below the long threshold
                side = 'long'
                strength = (long_thr - latest_rsi) / long_thr if long_thr > 0 else 0.5
            elif ema_ok_short and latest_rsi > short_thr:
                # SHORT: (downtrend) RSI pushed above the short threshold
                side = 'short'
                denom = (100.0 - short_thr) or 1.0
                strength = (latest_rsi - short_thr) / denom

            if side is None:
                # Explain which filter blocked entry, with live indicator values.
                if require_ema and current_price <= latest_ema and latest_rsi < long_thr:
                    why = (f'RSI<{long_thr:.0f} (long) but EMA trend filter blocked: '
                           f'price {ema_status} (need above for long)')
                elif require_ema and current_price >= latest_ema and latest_rsi > short_thr:
                    why = (f'RSI>{short_thr:.0f} (short) but EMA trend filter blocked: '
                           f'price {ema_status} (need below for short)')
                elif ema_ok_long and latest_rsi >= long_thr and latest_rsi <= short_thr:
                    why = (f'RSI filter: rsi={latest_rsi:.1f} not extreme '
                           f'(need <{long_thr:.0f} long / >{short_thr:.0f} short)')
                else:
                    why = (f'no entry: rsi={latest_rsi:.1f}, price {ema_status} '
                           f'(thresholds <{long_thr:.0f}/>{short_thr:.0f})')
                logger.info(
                    'SIGNAL_REJECTED %s | stage=filter | rsi=%.1f ema=%s price=%.4f ema200=%.4f '
                    'regime=%s vol=%s | %s',
                    symbol, latest_rsi, ema_status, current_price, latest_ema,
                    regime.value, volatility.value, why,
                )
                return None

            # ── BTC regime filter (market-direction gate) ──
            # UP_IMPULSE → only LONG, DOWN_IMPULSE → only SHORT, SIDEWAYS → both.
            # Fail-safe: UNKNOWN (BTC data unavailable) allows both directions.
            if self.settings.btc_regime_filter_enabled:
                btc_regime = self.get_btc_regime()
                if not regime_allows_side(btc_regime, side):
                    logger.info(
                        'SIGNAL_REJECTED %s | stage=btc_regime | side=%s blocked by '
                        'BTC regime=%s (rsi=%.1f price=%.4f)',
                        symbol, side.upper(), btc_regime.value.upper(),
                        latest_rsi, current_price,
                    )
                    return None

            # Clamp strength
            strength = max(0.1, min(1.0, strength))

            signal = Signal(
                symbol=symbol,
                side=side,
                strength=strength,
                atr=latest_atr,
                market_regime=regime.value,
                volatility=volatility.value,
                current_price=current_price,
                ema200=latest_ema,
                rsi=latest_rsi,
                timestamp=time.time(),
            )

            logger.info(
                'SIGNAL_FOUND %s %s | rsi=%.1f ema=%s price=%.4f ema200=%.4f '
                'atr=%.6f regime=%s vol=%s strength=%.2f (thresholds <%.0f/>%.0f)',
                signal.side.upper(), symbol, latest_rsi, ema_status, current_price,
                latest_ema, latest_atr, regime.value, volatility.value, strength,
                long_thr, short_thr,
            )
            return signal

        except Exception as e:
            logger.warning('Signal generation failed for %s: %s', symbol, e)
            logger.info('SIGNAL_REJECTED %s | stage=error | %s', symbol, e)
            return None
