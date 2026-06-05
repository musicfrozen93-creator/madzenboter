"""
Zentry Futures Core — Signal Engine.

Generates entry signals using RSI + EMA200 and classifies market
regime using ADX (for regime only, never for entry decisions).
"""

import logging
import time
from typing import Optional, Tuple

import pandas as pd

from config.settings import MarketRegime, Settings, VolatilityLevel
from core.dto import Signal
from exchange.client import ExchangeClient
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
                logger.debug(
                    '%s: insufficient 1h data (%d bars, need %d)',
                    symbol, len(df_1h), self.settings.ema_period,
                )
                return None

            df_5m = self.exchange.fetch_ohlcv(
                symbol, self.settings.signal_timeframe, limit=100
            )
            if len(df_5m) < self.settings.rsi_period + 5:
                logger.debug('%s: insufficient 5m data', symbol)
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

            # ── Entry conditions (RSI + EMA200 ONLY) ──
            side: Optional[str] = None
            strength: float = 0.0

            if current_price > latest_ema and latest_rsi < self.settings.rsi_oversold:
                # LONG: price above trend, RSI oversold
                side = 'long'
                strength = (self.settings.rsi_oversold - latest_rsi) / self.settings.rsi_oversold
            elif current_price < latest_ema and latest_rsi > self.settings.rsi_overbought:
                # SHORT: price below trend, RSI overbought
                side = 'short'
                strength = (latest_rsi - self.settings.rsi_overbought) / (
                    100.0 - self.settings.rsi_overbought
                )

            if side is None:
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
                'SIGNAL: %s %s | price=%.4f ema200=%.4f rsi=%.1f '
                'atr=%.6f regime=%s vol=%s strength=%.2f',
                signal.side.upper(), symbol, current_price, latest_ema,
                latest_rsi, latest_atr, regime.value, volatility.value, strength,
            )
            return signal

        except Exception as e:
            logger.warning('Signal generation failed for %s: %s', symbol, e)
            return None
