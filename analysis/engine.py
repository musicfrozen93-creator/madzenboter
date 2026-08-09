"""Analysis Engine — stage 1 of the signal pipeline.

Turns standardized market data for ONE timeframe into a `TechnicalPicture`:
every indicator reading, the regime classification, market structure, support and
resistance, Fibonacci geometry, the Elliott wave count, and the market-quality
verdict.

The engine consumes a `MarketDataProvider`. It has NO knowledge of Binance or
any other venue, so a new market is added by writing a provider, not by touching
this file.

Every indicator comes from `signals.indicators` / `v4.indicators` — the single
source of truth for technical calculations. Nothing is reimplemented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from analysis.cache import RequestScope
from analysis.elliott import ElliottState, analyze_elliott
from analysis.fibonacci import FibonacciState, analyze_fibonacci
from analysis.levels import SupportResistance, detect_levels
from analysis.smc import (
    ADXState,
    FairValueGapState,
    LiquidityState,
    MACDState,
    OrderBlockState,
    PatternState,
    VWAPState,
    compute_adx_state,
    compute_macd,
    compute_vwap,
    detect_fair_value_gaps,
    detect_liquidity,
    detect_order_blocks,
    detect_patterns,
)
from analysis.structure import BEARISH, BULLISH, RANGE, StructureState, analyze_structure
from config.settings import BtcRegime, Settings
from providers.base import MarketDataProvider, Quote
from signals.btc_regime import classify_btc_regime
from signals.filters import FilterResult, evaluate_market_quality, first_failure
from signals.indicators import (
    compute_atr,
    compute_bollinger_bands,
    compute_ema,
    compute_rsi,
)
from v4.indicators import bollinger_bandwidth, compute_adx
from v4.params import V4Params
from v4.regime import Direction, RegimeEngine, RegimeState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndicatorSnapshot:
    """Every indicator reading on the last closed candle."""

    price: float
    rsi: float
    atr: float
    atr_pct: float
    atr_average_pct: float
    ema_fast: float
    ema_slow: float
    adx: float
    plus_di: float
    minus_di: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    bb_bandwidth: float
    volume: float
    volume_average: float

    @property
    def volume_ratio(self) -> float:
        """Last volume as a multiple of its recent average (0.0 if unknown)."""
        return (self.volume / self.volume_average) if self.volume_average > 0 else 0.0

    @property
    def volatility_label(self) -> str:
        """Current ATR relative to its own recent average, in plain words."""
        if self.atr_average_pct <= 0:
            return 'unknown'
        ratio = self.atr_pct / self.atr_average_pct
        if ratio >= 1.5:
            return 'elevated'
        if ratio <= 0.6:
            return 'compressed'
        return 'normal'

    @property
    def rsi_label(self) -> str:
        """RSI zone, in plain words."""
        if self.rsi >= 70:
            return 'overbought'
        if self.rsi >= 55:
            return 'bullish'
        if self.rsi > 45:
            return 'neutral'
        if self.rsi > 30:
            return 'bearish'
        return 'oversold'


@dataclass
class TechnicalPicture:
    """Everything the downstream stages need about one symbol/timeframe."""

    market: str
    provider: str
    symbol: str
    timeframe: str
    candles: pd.DataFrame                    # standardized OHLCV, oldest first
    indicators: IndicatorSnapshot
    regime: RegimeState
    structure: StructureState
    levels: SupportResistance
    fibonacci: FibonacciState
    elliott: ElliottState
    # Smart Money confirmation layers (Phase 2)
    order_blocks: OrderBlockState
    fair_value_gaps: FairValueGapState
    liquidity: LiquidityState
    vwap: VWAPState
    macd: MACDState
    adx: ADXState
    patterns: PatternState
    quote: Optional[Quote] = None
    benchmark_symbol: Optional[str] = None
    benchmark_regime: BtcRegime = BtcRegime.UNKNOWN
    benchmark_candles: Optional[pd.DataFrame] = None
    quality_filters: List[FilterResult] = field(default_factory=list)

    @property
    def market_conditions_clean(self) -> bool:
        """True when no market-quality filter tripped."""
        return first_failure(self.quality_filters) is None

    @property
    def blocking_condition(self) -> Optional[str]:
        """Human-readable reason conditions are unclean, or None."""
        failure = first_failure(self.quality_filters)
        return f'{failure.name}: {failure.detail}' if failure else None

    @property
    def trend_direction(self) -> str:
        """The timeframe's directional read: 'bullish' | 'bearish' | 'range'.

        Combines three independent votes — the regime router, the EMA stack, and
        market structure — so a single noisy input cannot flip the read.
        """
        votes: List[str] = []

        if self.regime.direction == Direction.TREND_UP:
            votes.append(BULLISH)
        elif self.regime.direction == Direction.TREND_DOWN:
            votes.append(BEARISH)

        ind = self.indicators
        if ind.ema_fast > 0 and ind.ema_slow > 0:
            if ind.ema_fast > ind.ema_slow and ind.price > ind.ema_slow:
                votes.append(BULLISH)
            elif ind.ema_fast < ind.ema_slow and ind.price < ind.ema_slow:
                votes.append(BEARISH)

        if self.structure.trend in (BULLISH, BEARISH):
            votes.append(self.structure.trend)

        bulls = votes.count(BULLISH)
        bears = votes.count(BEARISH)
        if bulls > bears:
            return BULLISH
        if bears > bulls:
            return BEARISH
        return RANGE

    @property
    def trend_conviction(self) -> float:
        """How unanimous the directional read is, 0–1."""
        direction = self.trend_direction
        if direction == RANGE:
            return 0.0
        agree = 0
        total = 3
        if self.regime.direction == (
            Direction.TREND_UP if direction == BULLISH else Direction.TREND_DOWN
        ):
            agree += 1
        ind = self.indicators
        if direction == BULLISH and ind.ema_fast > ind.ema_slow and ind.price > ind.ema_slow:
            agree += 1
        elif direction == BEARISH and ind.ema_fast < ind.ema_slow and ind.price < ind.ema_slow:
            agree += 1
        if self.structure.trend == direction:
            agree += 1
        return agree / total


class AnalysisEngine:
    """Builds a TechnicalPicture from a market data provider."""

    def __init__(
        self,
        settings: Settings,
        params: Optional[V4Params] = None,
        regime_engine: Optional[RegimeEngine] = None,
    ) -> None:
        self.settings = settings
        self.params = params or V4Params()
        self.regime_engine = regime_engine or RegimeEngine(self.params)

    # ── Public API ──

    def analyze(
        self,
        provider: MarketDataProvider,
        symbol: str,
        timeframe: str,
        candle_limit: Optional[int] = None,
        scope: Optional[RequestScope] = None,
        include_benchmark: bool = True,
        include_quote: bool = True,
    ) -> TechnicalPicture:
        """Fetch data and compute the full technical picture for one timeframe.

        Args:
            provider: The market data source.
            symbol: Platform symbol.
            timeframe: Candle interval.
            candle_limit: Bars to request (defaults to the configured limit).
            scope: Per-request cache. One is created if omitted.
            include_benchmark: Fetch the market benchmark for systemic context.
                The multi-timeframe engine enables this only on the highest
                timeframe, since systemic risk is not a low-timeframe concept.
            include_quote: Fetch the live quote for spread analysis.

        Raises:
            providers.base.ProviderError: On any market-data failure.
            ValueError: If there is not enough history to compute indicators.
        """
        limit = candle_limit or self.settings.candle_limit
        interval = provider.ensure_timeframe(timeframe)
        scope = scope or RequestScope()

        candles = scope.candles(
            provider.name, symbol, interval, limit,
            lambda: provider.fetch_candles(symbol, interval, limit=limit),
        )
        required = self._min_bars()
        if len(candles) < required:
            raise ValueError(
                f'{symbol} {interval}: {len(candles)} candles available, '
                f'{required} needed for the full indicator set'
            )

        quote = self._safe_quote(provider, symbol) if include_quote else None
        indicators = self._compute_indicators(candles)

        benchmark_symbol = provider.reference_symbol()
        benchmark_candles = None
        benchmark_regime = BtcRegime.UNKNOWN
        if include_benchmark and benchmark_symbol:
            benchmark_candles = self._safe_benchmark(
                provider, benchmark_symbol, interval, limit, scope
            )
            benchmark_regime = self._classify_benchmark(benchmark_candles)

        regime = self.regime_engine.classify(candles, benchmark_candles)

        # ── Structure-derived modules. Each is pure and reuses the swings, so
        #    pivots are detected exactly once per timeframe. ──
        structure = analyze_structure(candles, indicators.atr)
        swings = structure.swings
        levels = detect_levels(swings, indicators.price, indicators.atr, len(candles))
        fibonacci = analyze_fibonacci(swings, indicators.price)
        elliott = analyze_elliott(swings, indicators.price)

        # ── Smart Money confirmation layers (Phase 2). All pure, all reuse the
        #    same candles and swings — no extra market-data fetch. ──
        order_blocks = detect_order_blocks(candles, swings, indicators.atr)
        fair_value_gaps = detect_fair_value_gaps(candles, indicators.atr)
        liquidity = detect_liquidity(candles, swings, indicators.atr)
        vwap = compute_vwap(candles, indicators.atr)
        macd = compute_macd(candles)
        adx = compute_adx_state(candles)
        patterns = detect_patterns(swings, indicators.price, indicators.atr)

        spread = quote.spread if quote else 0.0
        price = quote.last if quote and quote.last > 0 else indicators.price
        quality_filters = evaluate_market_quality(
            candles, indicators.atr, spread, price, self.settings
        )

        return TechnicalPicture(
            market=provider.market,
            provider=provider.name,
            symbol=symbol,
            timeframe=interval,
            candles=candles,
            indicators=indicators,
            regime=regime,
            structure=structure,
            levels=levels,
            fibonacci=fibonacci,
            elliott=elliott,
            order_blocks=order_blocks,
            fair_value_gaps=fair_value_gaps,
            liquidity=liquidity,
            vwap=vwap,
            macd=macd,
            adx=adx,
            patterns=patterns,
            quote=quote,
            benchmark_symbol=benchmark_symbol,
            benchmark_regime=benchmark_regime,
            benchmark_candles=benchmark_candles,
            quality_filters=quality_filters,
        )

    # ── Internals ──

    def _min_bars(self) -> int:
        """Bars needed for the slowest indicator in the set to warm up."""
        p = self.params
        s = self.settings
        return max(
            p.ema_slow,
            p.bb_period + p.squeeze_lookback,
            p.adx_period * 2,
            s.bb_period,
            s.rsi_period,
            s.atr_period,
            s.risk_filter_lookback,
        ) + 2

    def _compute_indicators(self, df: pd.DataFrame) -> IndicatorSnapshot:
        """Compute every indicator reading on the last closed candle."""
        p = self.params
        s = self.settings
        close, high, low = df['close'], df['high'], df['low']

        rsi = compute_rsi(close, period=s.rsi_period)
        atr = compute_atr(high, low, close, period=s.atr_period)
        middle, upper, lower = compute_bollinger_bands(
            close, period=s.bb_period, num_std=s.bb_std
        )
        bandwidth = bollinger_bandwidth(close, period=p.bb_period, num_std=p.bb_std)
        ema_fast = compute_ema(close, period=p.ema_fast)
        ema_slow = compute_ema(close, period=p.ema_slow)
        adx, plus_di, minus_di = compute_adx(high, low, close, period=p.adx_period)

        lookback = max(5, s.risk_filter_lookback)
        recent_volume = df['volume'].iloc[-(lookback + 1):-1]

        price = _last(close)
        atr_value = _last(atr)

        # Average ATR% over the lookback tells us whether current volatility is
        # unusual FOR THIS MARKET, rather than against an arbitrary constant.
        atr_series = atr.dropna()
        if len(atr_series) >= lookback and price > 0:
            atr_average_pct = float(atr_series.iloc[-lookback:].mean()) / price
        else:
            atr_average_pct = (atr_value / price) if price > 0 else 0.0

        return IndicatorSnapshot(
            price=price,
            rsi=_last(rsi),
            atr=atr_value,
            atr_pct=(atr_value / price) if price > 0 else 0.0,
            atr_average_pct=atr_average_pct,
            ema_fast=_last(ema_fast),
            ema_slow=_last(ema_slow),
            adx=_last(adx),
            plus_di=_last(plus_di),
            minus_di=_last(minus_di),
            bb_upper=_last(upper),
            bb_middle=_last(middle),
            bb_lower=_last(lower),
            bb_bandwidth=_last(bandwidth),
            volume=float(df['volume'].iloc[-1]),
            volume_average=float(recent_volume.mean()) if not recent_volume.empty else 0.0,
        )

    def _safe_quote(
        self, provider: MarketDataProvider, symbol: str
    ) -> Optional[Quote]:
        """Fetch the live quote; a failure degrades gracefully to candle close."""
        try:
            return provider.fetch_quote(symbol)
        except Exception as exc:
            logger.warning('Quote unavailable for %s: %s', symbol, exc)
            return None

    def _safe_benchmark(
        self,
        provider: MarketDataProvider,
        benchmark_symbol: str,
        timeframe: str,
        limit: int,
        scope: RequestScope,
    ) -> Optional[pd.DataFrame]:
        """Fetch benchmark candles; missing context is never fatal."""
        try:
            return scope.candles(
                provider.name, benchmark_symbol, timeframe, limit,
                lambda: provider.fetch_candles(benchmark_symbol, timeframe, limit=limit),
            )
        except Exception as exc:
            logger.warning(
                'Benchmark %s unavailable: %s — continuing without market context',
                benchmark_symbol, exc,
            )
            return None

    def _classify_benchmark(self, df: Optional[pd.DataFrame]) -> BtcRegime:
        """Classify broad-market direction from the benchmark's EMA structure."""
        if df is None:
            return BtcRegime.UNKNOWN
        return classify_btc_regime(df, self.settings)


def _last(series: pd.Series) -> float:
    """Last non-NaN value of a series, or 0.0 when it never warmed up."""
    cleaned = series.dropna()
    return float(cleaned.iloc[-1]) if not cleaned.empty else 0.0
