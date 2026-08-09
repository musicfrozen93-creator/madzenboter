"""Regime Detection Engine (spec §C3).

Classifies a symbol's higher-timeframe state along independent axes and routes to
the engine that owns that state — or to FLAT. The intelligence of V4 is choosing
*when* each engine applies and *when none does*; "no trade" is a first-class
outcome (spec §2, §C3).

Axes:
  • Direction  — TREND_UP / TREND_DOWN / RANGE   (ADX + EMA structure + price)
  • Volatility — EXPANSION / NORMAL / CONTRACTION (ATR/bandwidth percentile)
  • Systemic   — BTC risk-off gate (undirected high-volatility in BTC ⇒ stand down)

Routing (spec §C3):
  RANGE + CONTRACTION + calm BTC          → 'range' engine (small sleeve)
  TREND_* + (EXPANSION or NORMAL)         → 'trend'  engine (primary)
  squeeze release (CONTRACTION→EXPANSION) → 'breakout' candidate flag
  anything uncertain / systemic risk-off  → None (FLAT)

Pure and network-free: callers pass in the already-fetched OHLCV DataFrames.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from signals.indicators import compute_atr, compute_ema
from v4.indicators import bollinger_bandwidth, compute_adx
from v4.params import V4Params


class Direction(str, Enum):
    TREND_UP = 'trend_up'
    TREND_DOWN = 'trend_down'
    RANGE = 'range'
    UNKNOWN = 'unknown'


class Volatility(str, Enum):
    EXPANSION = 'expansion'
    NORMAL = 'normal'
    CONTRACTION = 'contraction'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class RegimeState:
    """Full regime classification for one symbol."""

    direction: Direction
    volatility: Volatility
    adx: float
    tradeable: bool
    armed_engine: Optional[str]   # 'trend' | 'breakout' | 'range' | None
    allowed_side: Optional[str]   # 'long' | 'short' | None (both when None+range)
    breakout_candidate: bool
    reason: str


def _min_bars(p: V4Params) -> int:
    return max(p.ema_slow, p.bb_period + p.squeeze_lookback, p.adx_period * 2) + 2


def _classify_direction(df: pd.DataFrame, p: V4Params) -> tuple[Direction, float]:
    """Direction from ADX strength + EMA alignment + price vs slow EMA."""
    adx_series, plus_di, minus_di = compute_adx(
        df['high'], df['low'], df['close'], period=p.adx_period
    )
    ema_fast = compute_ema(df['close'], period=p.ema_fast)
    ema_slow = compute_ema(df['close'], period=p.ema_slow)

    adx = adx_series.dropna()
    if adx.empty or ema_fast.dropna().empty or ema_slow.dropna().empty:
        return Direction.UNKNOWN, 0.0

    adx_val = float(adx.iloc[-1])
    price = float(df['close'].iloc[-1])
    ef = float(ema_fast.iloc[-1])
    es = float(ema_slow.iloc[-1])
    pdi = float(plus_di.iloc[-1])
    mdi = float(minus_di.iloc[-1])

    if pd.isna(adx_val) or pd.isna(ef) or pd.isna(es):
        return Direction.UNKNOWN, 0.0

    trending = adx_val >= p.adx_trend_min
    ranging = adx_val <= p.adx_range_max

    if trending and price > es and ef > es and pdi > mdi:
        return Direction.TREND_UP, adx_val
    if trending and price < es and ef < es and mdi > pdi:
        return Direction.TREND_DOWN, adx_val
    if ranging:
        return Direction.RANGE, adx_val
    # ADX in the 20–25 no-man's-land, or misaligned EMAs during a trend read.
    return Direction.RANGE if not trending else Direction.UNKNOWN, adx_val


def _classify_volatility(df: pd.DataFrame, p: V4Params) -> tuple[Volatility, bool]:
    """Volatility regime + squeeze-release flag from Bollinger bandwidth percentile."""
    bw = bollinger_bandwidth(df['close'], period=p.bb_period, num_std=p.bb_std).dropna()
    if len(bw) < p.squeeze_lookback:
        return Volatility.UNKNOWN, False

    window = bw.iloc[-p.squeeze_lookback:]
    current = float(bw.iloc[-1])
    prev = float(bw.iloc[-2]) if len(bw) >= 2 else current
    low_thresh = float(window.quantile(p.squeeze_percentile))
    high_thresh = float(window.quantile(1.0 - p.squeeze_percentile))

    # Squeeze release: bandwidth was compressed last bar and is now expanding.
    was_squeezed = prev <= low_thresh
    expanding = current > prev
    breakout_candidate = was_squeezed and expanding and current > low_thresh

    if current <= low_thresh:
        return Volatility.CONTRACTION, breakout_candidate
    if current >= high_thresh:
        return Volatility.EXPANSION, breakout_candidate
    return Volatility.NORMAL, breakout_candidate


class RegimeEngine:
    """Stateless regime classifier + engine router (spec §C3)."""

    def __init__(self, params: Optional[V4Params] = None) -> None:
        self.params = params or V4Params()

    def _btc_risk_off(self, df_btc: Optional[pd.DataFrame]) -> bool:
        """Systemic gate: BTC in undirected high volatility ⇒ stand down.

        Undirected = EXPANSION volatility while direction is not a clean trend.
        A clean BTC trend is NOT risk-off (the trend engine can ride it).
        """
        if df_btc is None or len(df_btc) < _min_bars(self.params):
            return False  # no BTC data ⇒ fail-safe, do not force risk-off here
        direction, _ = _classify_direction(df_btc, self.params)
        vol, _ = _classify_volatility(df_btc, self.params)
        undirected = direction in (Direction.RANGE, Direction.UNKNOWN)
        return vol == Volatility.EXPANSION and undirected

    def classify(
        self, df: pd.DataFrame, df_btc: Optional[pd.DataFrame] = None
    ) -> RegimeState:
        """Classify a symbol's HTF regime and route to an engine (or FLAT)."""
        p = self.params
        if df is None or len(df) < _min_bars(p):
            return RegimeState(
                Direction.UNKNOWN, Volatility.UNKNOWN, 0.0, False, None, None,
                False, 'insufficient_data',
            )

        if self._btc_risk_off(df_btc):
            direction, adx_val = _classify_direction(df, p)
            vol, _ = _classify_volatility(df, p)
            return RegimeState(
                direction, vol, adx_val, False, None, None, False,
                'systemic_risk_off (BTC undirected high volatility)',
            )

        direction, adx_val = _classify_direction(df, p)
        vol, breakout_candidate = _classify_volatility(df, p)

        if direction == Direction.UNKNOWN or vol == Volatility.UNKNOWN:
            return RegimeState(
                direction, vol, adx_val, False, None, None, breakout_candidate,
                'uncertain_regime',
            )

        # ── Routing ──
        if direction in (Direction.TREND_UP, Direction.TREND_DOWN) and vol in (
            Volatility.EXPANSION, Volatility.NORMAL
        ):
            side = 'long' if direction == Direction.TREND_UP else 'short'
            engine = 'breakout' if breakout_candidate else 'trend'
            return RegimeState(
                direction, vol, adx_val, True, engine, side, breakout_candidate,
                f'{engine}_armed ({direction.value}, {vol.value})',
            )

        if direction == Direction.RANGE and vol == Volatility.CONTRACTION:
            return RegimeState(
                direction, vol, adx_val, True, 'range', None, breakout_candidate,
                'range_armed (contraction)',
            )

        return RegimeState(
            direction, vol, adx_val, False, None, None, breakout_candidate,
            f'no_engine_for_regime ({direction.value}, {vol.value})',
        )
