"""
ZenGrid — Signal Engine (Dark-Venus mean reversion).

Generates mean-reversion entry signals on the 15m timeframe:

  LONG  : RSI(14) < 30  AND price touches the LOWER Bollinger band  AND BTC filter approves
  SHORT : RSI(14) > 70  AND price touches the UPPER Bollinger band  AND BTC filter approves

Before any signal is emitted, a set of pre-trade risk-rule filters can SKIP the
trade (spread too high, ATR explosion, news candle, oversized candle body, or a
volume spike). The BTC 15m trend filter gates the allowed direction and runs
before every new basket.
"""

import logging
import time
from typing import Optional

import pandas as pd

from config.settings import BtcRegime, Settings
from core.dto import Signal
from exchange.client import ExchangeClient
from signals.btc_regime import classify_btc_regime, regime_allows_side
from signals.indicators import (
    compute_atr,
    compute_bollinger_bands,
    compute_rsi,
)

logger = logging.getLogger(__name__)


class SignalEngine:
    """Generates mean-reversion entry signals for the supported symbols.

    Entry logic (all conditions must hold):
      LONG  → RSI < rsi_oversold   AND candle low  <= lower Bollinger band
      SHORT → RSI > rsi_overbought AND candle high >= upper Bollinger band
    plus the BTC trend filter must permit the direction.

    Pre-trade skip filters (any one trips → skip with a logged reason):
      • spread too high          (spread / price > max_spread_pct)
      • ATR explosion            (current ATR > atr_explosion_multiplier × avg ATR)
      • news / oversized candle  (candle body > news_candle_atr_multiplier × ATR)
      • volume spike             (last volume > volume_spike_multiplier × avg volume)
    """

    def __init__(self, exchange_client: ExchangeClient, settings: Settings) -> None:
        self.exchange = exchange_client
        self.settings = settings
        # Cached BTC regime (recomputed at most every btc_regime_cache_seconds).
        self._btc_regime: BtcRegime = BtcRegime.UNKNOWN
        self._btc_regime_ts: float = 0.0

    # ───────────────────────────────────────────
    # BTC trend filter
    # ───────────────────────────────────────────

    def get_btc_regime(self) -> BtcRegime:
        """Return the current BTC 15m regime, cached for btc_regime_cache_seconds.

        On any failure it fails safe by returning UNKNOWN (which allows trading
        in both directions).
        """
        now = time.time()
        ttl = max(0, self.settings.btc_regime_cache_seconds)
        if self._btc_regime_ts and (now - self._btc_regime_ts) < ttl:
            return self._btc_regime

        try:
            df_btc = self.exchange.fetch_ohlcv(
                self.settings.btc_symbol, self.settings.timeframe,
                limit=self.settings.candle_limit,
            )
            regime = classify_btc_regime(df_btc, self.settings)
        except Exception as e:
            logger.warning(
                'BTC regime fetch failed for %s: %s — failing safe (UNKNOWN, '
                'both directions allowed)', self.settings.btc_symbol, e,
            )
            regime = BtcRegime.UNKNOWN

        self._btc_regime = regime
        self._btc_regime_ts = now
        logger.info('BTC_FILTER | %s (cached %ds)', regime.value.upper(), ttl)
        return regime

    # ───────────────────────────────────────────
    # Pre-trade risk-rule skip filters
    # ───────────────────────────────────────────

    def _risk_filter_reason(
        self, df: pd.DataFrame, atr: float, spread: float, price: float
    ) -> Optional[str]:
        """Return a skip reason if any pre-trade risk filter trips, else None."""
        s = self.settings

        # Spread too high
        if price > 0 and spread > 0:
            spread_pct = spread / price
            if spread_pct > s.max_spread_pct:
                return f'spread_too_high ({spread_pct:.4%} > {s.max_spread_pct:.4%})'

        lookback = max(5, s.risk_filter_lookback)

        # ATR explosion (current ATR vs recent average ATR)
        atr_series = compute_atr(df['high'], df['low'], df['close'], period=s.atr_period).dropna()
        if len(atr_series) >= lookback:
            avg_atr = float(atr_series.iloc[-lookback:].mean())
            if avg_atr > 0 and atr > s.atr_explosion_multiplier * avg_atr:
                return (
                    f'atr_explosion (atr={atr:.6f} > {s.atr_explosion_multiplier:.1f}× '
                    f'avg={avg_atr:.6f})'
                )

        # News / oversized candle (last candle body vs ATR)
        last = df.iloc[-1]
        body = abs(float(last['close']) - float(last['open']))
        if atr > 0 and body > s.news_candle_atr_multiplier * atr:
            return (
                f'news_candle (body={body:.6f} > {s.news_candle_atr_multiplier:.1f}× '
                f'atr={atr:.6f})'
            )

        # Volume spike (last volume vs recent average volume)
        if 'volume' in df.columns and len(df) >= lookback + 1:
            recent = df['volume'].iloc[-(lookback + 1):-1]
            avg_vol = float(recent.mean()) if not recent.empty else 0.0
            last_vol = float(last['volume'])
            if avg_vol > 0 and last_vol > s.volume_spike_multiplier * avg_vol:
                return (
                    f'volume_spike (vol={last_vol:.0f} > {s.volume_spike_multiplier:.1f}× '
                    f'avg={avg_vol:.0f})'
                )

        return None

    # ───────────────────────────────────────────
    # Correlation-protection signal-strength score (0–4)
    # ───────────────────────────────────────────

    def _strength_score(
        self, df, side, rsi, price, candle_low, candle_high,
        bb_lower, bb_upper, btc_regime, spread,
    ):
        """Score the setup 0–4 for the correlation second-symbol rule.

        +1 RSI extreme (< 20 or > 80)
        +1 strong Bollinger penetration (the CLOSE pierces the band, not just a wick)
        +1 BTC trend strongly aligned with the trade direction
        +1 good spread AND liquidity (tight spread + healthy volume)
        """
        s = self.settings
        parts = []
        score = 0

        # 1) Extreme RSI
        if rsi < 20 or rsi > 80:
            score += 1
            parts.append('rsi_extreme')

        # 2) Strong Bollinger penetration — close beyond the band
        if (side == 'long' and price <= bb_lower) or (side == 'short' and price >= bb_upper):
            score += 1
            parts.append('bb_penetration')

        # 3) BTC strongly aligned
        if (side == 'long' and btc_regime == BtcRegime.BULLISH) or \
           (side == 'short' and btc_regime == BtcRegime.BEARISH):
            score += 1
            parts.append('btc_aligned')

        # 4) Good spread AND liquidity
        spread_pct = (spread / price) if price > 0 else 1.0
        lookback = max(5, s.risk_filter_lookback)
        good_liquidity = False
        if 'volume' in df.columns and len(df) >= lookback + 1:
            recent = df['volume'].iloc[-(lookback + 1):-1]
            avg_vol = float(recent.mean()) if not recent.empty else 0.0
            last_vol = float(df['volume'].iloc[-1])
            good_liquidity = avg_vol > 0 and last_vol >= avg_vol
        if spread_pct < (s.max_spread_pct * 0.5) and good_liquidity:
            score += 1
            parts.append('spread_liquidity')

        return score, ','.join(parts) if parts else 'none'

    # ───────────────────────────────────────────
    # Signal generation
    # ───────────────────────────────────────────

    def generate_signal(self, symbol: str) -> Optional[Signal]:
        """Generate a mean-reversion entry signal for a symbol (15m).

        Returns:
            Signal if all entry conditions hold and no risk filter trips,
            None otherwise (the reason is logged as SIGNAL_SKIP).
        """
        s = self.settings

        # Only the supported symbols are ever traded.
        if not s.is_supported_symbol(symbol):
            logger.info(
                'SIGNAL_SKIP | symbol=%s reason=unsupported_symbol', symbol
            )
            return None

        try:
            df = self.exchange.fetch_ohlcv(symbol, s.timeframe, limit=s.candle_limit)
            min_bars = max(s.bb_period, s.rsi_period, s.atr_period) + s.risk_filter_lookback + 2
            if len(df) < min_bars:
                logger.info(
                    'SIGNAL_SKIP | symbol=%s reason=insufficient_data (%d bars, need %d)',
                    symbol, len(df), min_bars,
                )
                return None

            # ── Indicators ──
            rsi = compute_rsi(df['close'], period=s.rsi_period).dropna()
            atr_series = compute_atr(
                df['high'], df['low'], df['close'], period=s.atr_period
            ).dropna()
            middle, upper, lower = compute_bollinger_bands(
                df['close'], period=s.bb_period, num_std=s.bb_std
            )

            if rsi.empty or atr_series.empty or lower.dropna().empty:
                logger.info('SIGNAL_SKIP | symbol=%s reason=indicator_warmup', symbol)
                return None

            latest = df.iloc[-1]
            current_price = float(latest['close'])
            candle_low = float(latest['low'])
            candle_high = float(latest['high'])
            latest_rsi = float(rsi.iloc[-1])
            latest_atr = float(atr_series.iloc[-1])
            bb_mid = float(middle.iloc[-1])
            bb_upper = float(upper.iloc[-1])
            bb_lower = float(lower.iloc[-1])

            if any(pd.isna(v) for v in (current_price, latest_rsi, latest_atr, bb_lower, bb_upper)):
                logger.info('SIGNAL_SKIP | symbol=%s reason=nan_indicator', symbol)
                return None

            # ── Spread (from ticker) ──
            spread = 0.0
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                spread = float(ticker.get('spread', 0) or 0)
                if ticker.get('last'):
                    current_price = float(ticker['last'])
            except Exception as e:
                logger.debug('Ticker fetch failed for %s: %s', symbol, e)

            # ── Pre-trade risk-rule skip filters ──
            skip = self._risk_filter_reason(df, latest_atr, spread, current_price)
            if skip:
                logger.info(
                    'SIGNAL_SKIP | symbol=%s reason=%s rsi=%.1f price=%.6f',
                    symbol, skip, latest_rsi, current_price,
                )
                return None

            # ── Mean-reversion entry conditions ──
            long_ok = latest_rsi < s.rsi_oversold and candle_low <= bb_lower
            short_ok = latest_rsi > s.rsi_overbought and candle_high >= bb_upper

            side: Optional[str] = None
            strength = 0.0
            reason = ''
            if long_ok:
                side = 'long'
                strength = (s.rsi_oversold - latest_rsi) / s.rsi_oversold if s.rsi_oversold > 0 else 0.5
                reason = (
                    f'RSI {latest_rsi:.1f} < {s.rsi_oversold:.0f} and price touched '
                    f'lower BB ({candle_low:.6f} <= {bb_lower:.6f})'
                )
            elif short_ok:
                side = 'short'
                denom = (100.0 - s.rsi_overbought) or 1.0
                strength = (latest_rsi - s.rsi_overbought) / denom
                reason = (
                    f'RSI {latest_rsi:.1f} > {s.rsi_overbought:.0f} and price touched '
                    f'upper BB ({candle_high:.6f} >= {bb_upper:.6f})'
                )

            if side is None:
                logger.info(
                    'SIGNAL_SKIP | symbol=%s reason=no_setup (rsi=%.1f price=%.6f '
                    'bb_lower=%.6f bb_upper=%.6f)',
                    symbol, latest_rsi, current_price, bb_lower, bb_upper,
                )
                return None

            # ── BTC trend filter (runs before every new basket) ──
            if s.btc_filter_enabled:
                btc_regime = self.get_btc_regime()
                if not regime_allows_side(btc_regime, side):
                    logger.info(
                        'SIGNAL_SKIP | symbol=%s direction=%s reason=btc_filter '
                        '(BTC %s blocks %s) rsi=%.1f price=%.6f',
                        symbol, side.upper(), btc_regime.value.upper(),
                        side.upper(), latest_rsi, current_price,
                    )
                    return None
            else:
                btc_regime = BtcRegime.UNKNOWN

            strength = max(0.1, min(1.0, strength))
            vol_label = 'high' if latest_atr > 0 and (latest_atr / current_price) > 0.01 else 'normal'

            # ── Correlation-protection signal-strength score (0–4) ──
            score, score_parts = self._strength_score(
                df, side, latest_rsi, current_price, candle_low, candle_high,
                bb_lower, bb_upper, btc_regime, spread,
            )

            signal = Signal(
                symbol=symbol,
                side=side,
                strength=strength,
                atr=latest_atr,
                market_regime=btc_regime.value,
                volatility=vol_label,
                current_price=current_price,
                ema200=bb_mid,
                rsi=latest_rsi,
                bb_lower=bb_lower,
                bb_upper=bb_upper,
                reason=reason,
                strength_score=score,
                timestamp=time.time(),
            )

            logger.info(
                'SIGNAL_FOUND | symbol=%s direction=%s rsi=%.1f price=%.6f atr=%.6f '
                'btc=%s score=%d/4 [%s] strength=%.2f | reason: %s',
                symbol, side.upper(), latest_rsi, current_price, latest_atr,
                btc_regime.value.upper(), score, score_parts, strength, reason,
            )
            return signal

        except Exception as e:
            logger.warning('Signal generation failed for %s: %s', symbol, e)
            logger.info('SIGNAL_SKIP | symbol=%s reason=error (%s)', symbol, e)
            return None
