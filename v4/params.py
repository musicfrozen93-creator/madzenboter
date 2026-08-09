"""Analysis parameters for the V4 regime/engine stack (spec §C2, §C3).

Every value the regime classifier, the entry engines, and the stop/target math
need lives here as the single source of truth.

These are the APPROVED defaults. They are intentionally NOT read from
config.json so that the V4 analysis stack can never silently pick up unrelated
values; overrides, if ever needed, are explicit constructor arguments.

Account auto-scaling (`AccountLimits` / `resolve_account_limits`) and the
position-sizing, leverage, concurrency, and circuit-breaker parameters that fed
it were removed in the Phase 0 conversion — nothing sizes or opens a position
any more.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class V4Params:
    """Immutable container of every finalized V4 analysis parameter (spec §C2)."""

    # ── Stop loss (spec §C2: structural + 1.5×ATR, floor 1%, ceiling 6%) ──
    stop_atr_mult: float = 1.5
    stop_floor_pct: float = 0.01          # never tighter than 1% (noise floor)
    stop_ceiling_pct: float = 0.06        # wider than 6% ⇒ reject the setup

    # ── Take profit (spec §C2) ──
    # The TP1/TP2/TP3 ladder is NOT built from R multiples — `analysis.generator`
    # derives every target from real levels (support/resistance zones, Fibonacci
    # extensions of the dominant leg, and the structural measured move). These
    # ratios only define the reward:risk floor a setup must clear.
    min_rr: float = 1.5                   # reject setups below this reward:risk
    target_rr: float = 2.0
    partial_trigger_rr: float = 1.5

    # ── Entry confluence gate (spec §A5, §C2: a confluence COUNT, deliberately
    #    not an uncalibrated 0–100 score) ──
    min_confluence: int = 4
    max_confluence: int = 6

    # ── Regime thresholds (spec §C3) ──
    adx_period: int = 14
    adx_trend_min: float = 25.0           # ADX ≥ this ⇒ trending
    adx_range_max: float = 20.0           # ADX ≤ this ⇒ ranging
    ema_fast: int = 50
    ema_slow: int = 200
    bb_period: int = 20
    bb_std: float = 2.0
    squeeze_lookback: int = 100           # bandwidth percentile window
    squeeze_percentile: float = 0.25      # bottom quartile bandwidth ⇒ squeeze

    # ── Engine parameters (spec §4, §C3) ──
    trend_pullback_ema: int = 20          # pullback reference MA for the trend engine
    swing_lookback: int = 20              # bars for swing high/low (stop anchor)
    breakout_lookback: int = 20           # bars defining the breakout range
    volume_lookback: int = 20             # bars for the average-volume baseline

    # ── Market-quality thresholds (spec §6, §C2) ──
    min_quote_volume: float = 100_000_000.0  # 24h quote volume floor (liquidity)
    scanner_max_spread_pct: float = 0.0005   # spread ceiling for eligibility (0.05%)
    funding_extreme_abs: float = 0.003       # |funding| above this ⇒ low quality
    min_atr_pct: float = 0.005               # below this ⇒ dead volatility
    max_atr_pct: float = 0.08                # above this ⇒ un-stoppable volatility

    # ── Data freshness (spec §C4) ──
    max_candle_age_seconds: int = 120        # stale market data ⇒ no signal

    # ── Costs (for expectancy / net checks; spec §A12) ──
    taker_fee_pct: float = 0.0004         # round-trip applied as 2× at exit
