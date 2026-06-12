"""
Zentry Futures Core — Signal Engine.

Generates entry signals using RSI + EMA200 and classifies market
regime using ADX (for regime only, never for entry decisions).
"""

import logging
import time
from typing import Dict, Optional, Tuple

import pandas as pd

from config.settings import MarketRegime, Settings, VolatilityLevel
from core.dto import Signal
from exchange.client import ExchangeClient
from signals.indicators import compute_adx, compute_atr, compute_ema, compute_rsi

logger = logging.getLogger(__name__)

# Absolute staleness ceiling for signal confirmation (C5 fix). Confirmation
# is counted in consecutive EVALUATIONS of a symbol (immune to watchlist
# pass duration); this wall-clock cap only rejects "consecutive" sightings
# separated by watchlist churn (symbol rotated out and back hours later).
_MAX_CONFIRMATION_GAP_SECONDS = 1800.0


class SignalEngine:
    """Generates entry signals and classifies market conditions.

    Entry Logic (strict RSI + EMA200 only):
      LONG:  price > EMA200 AND RSI < 30
      SHORT: price < EMA200 AND RSI > 70

    Market Classification (ADX + ATR):
      Regime:     ADX > 25 → TRENDING, ADX < 20 → SIDEWAYS
      Volatility: ATR vs 30-bar avg ATR → LOW / MEDIUM / HIGH
    """

    def __init__(
        self,
        exchange_client: ExchangeClient,
        settings: Settings,
        symbol_state_engine=None,
        market_state_engine=None,
    ) -> None:
        """Initialise the signal engine.

        Args:
            exchange_client: Exchange client for fetching OHLCV data.
            settings: Application settings.
            symbol_state_engine: V2 SymbolStateEngine — classifies hysteresis
                trend states on every evaluation (feeds breadth + routing).
                Optional; None = V1-equivalent signals.
            market_state_engine: V2 MarketStateEngine — supplies the BTC
                factor state and the BTC 1h frame for relative strength.
        """
        self.exchange = exchange_client
        self.settings = settings
        self.symbol_state_engine = symbol_state_engine
        self.market_state_engine = market_state_engine
        # Debounce store: (symbol, side) -> (count, eval_index, last ts).
        # A raw signal must persist across consecutive EVALUATIONS of the
        # symbol before it is emitted — kills intra-candle phantom signals
        # that never exist on any closed chart. Consecutiveness is counted
        # in evaluation passes (C5 fix), so the debounce works identically
        # whether a full watchlist pass takes 10 seconds or 5 minutes.
        self._pending: Dict[Tuple[str, str], Tuple[int, int, float]] = {}
        # Per-symbol evaluation counter (incremented on every generate_signal
        # call for the symbol, signal or not).
        self._eval_index: Dict[str, int] = {}

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
        # Count this evaluation pass for debounce consecutiveness (C5).
        self._eval_index[symbol] = self._eval_index.get(symbol, 0) + 1

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

            # ── V2: classify the symbol's hysteresis trend state ──
            # Runs on EVERY evaluation (not just on signals) so the cached
            # states feed market breadth and basket premise monitoring.
            symbol_state = 'unknown'
            relative_strength = 0.0
            btc_state = 'unknown'
            if self.symbol_state_engine is not None:
                btc_df = (
                    self.market_state_engine.get_btc_df_1h()
                    if self.market_state_engine is not None else None
                )
                snapshot = self.symbol_state_engine.classify(symbol, df_1h, btc_df)
                symbol_state = snapshot.state
                relative_strength = snapshot.relative_strength
            if self.market_state_engine is not None:
                btc_state = self.market_state_engine.get_state().btc_state

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
                # No raw candidate — reset any pending debounce streaks so a
                # later re-appearance starts a fresh confirmation count.
                self._pending.pop((symbol, 'long'), None)
                self._pending.pop((symbol, 'short'), None)
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

            # Clamp strength
            strength = max(0.1, min(1.0, strength))

            # ── V2: signal persistence / debouncing ──
            # The raw candidate must be observed on signal_confirmations
            # consecutive evaluations (within the window) before emitting.
            if not self._confirm_signal(symbol, side):
                return None

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
                symbol_state=symbol_state,
                btc_state=btc_state,
                relative_strength=relative_strength,
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

    def _confirm_signal(self, symbol: str, side: str) -> bool:
        """Debounce: require the raw signal to persist across consecutive
        EVALUATIONS of the symbol before emission (C5 fix).

        The original wall-clock window (90s) silently suppressed signals
        whenever a full watchlist pass took longer than the window — which
        the 50-symbol watchlist makes routine. Consecutiveness is now
        counted in per-symbol evaluation passes, so the debounce behaves
        identically at any watchlist size or pass duration. A generous
        wall-clock ceiling remains only to reject sightings separated by
        watchlist churn (symbol rotated out and back hours later).

        Args:
            symbol: Trading pair.
            side: Candidate side ('long' or 'short').

        Returns:
            True when the signal has been confirmed and should be emitted.
        """
        required = max(1, int(self.settings.signal_confirmations))
        if required <= 1:
            return True

        now = time.time()
        eval_idx = self._eval_index.get(symbol, 0)
        key = (symbol, side)
        # A candidate on one side resets the opposite side's streak.
        opposite = (symbol, 'short' if side == 'long' else 'long')
        self._pending.pop(opposite, None)

        count, last_idx, last_ts = self._pending.get(key, (0, -2, 0.0))
        max_gap = max(
            float(self.settings.signal_confirmation_window_seconds),
            _MAX_CONFIRMATION_GAP_SECONDS,
        )
        consecutive = (last_idx == eval_idx - 1) and (now - last_ts <= max_gap)
        count = count + 1 if consecutive else 1
        self._pending[key] = (count, eval_idx, now)

        if count < required:
            logger.info(
                'SIGNAL_PENDING %s %s | confirmation %d/%d (eval #%d)',
                side.upper(), symbol, count, required, eval_idx,
            )
            return False

        self._pending.pop(key, None)
        return True
