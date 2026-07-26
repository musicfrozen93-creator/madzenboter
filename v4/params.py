"""V4 parameters and per-account auto-scaling (spec §C1, §C2, §C5).

Every trading value in the V4 Final Specification lives here as the single source
of truth. The `resolve_account_limits` function turns a live account equity into
the concrete per-account limits, implementing the auto-scaling rules with ZERO
manual per-user configuration required (spec §C5): the same logic applies to a
$50 account and a $50,000 account — only the numbers differ, because everything
is expressed as a function of live equity.

These are the APPROVED defaults. They are intentionally NOT read from the V1
config.json so that turning on V4 can never silently pick up V1 tier values;
overrides, if ever needed, are explicit constructor arguments.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class V4Params:
    """Immutable container of every finalized V4 parameter (spec §C2)."""

    # ── Account viability (spec §A8, §C2) ──
    min_balance: float = 50.0            # hard floor — below this, no new trades
    recommended_min_balance: float = 100.0

    # ── Risk per trade (spec §C2: flat 1%, small-account bump to 1.5% ONLY to
    #    clear exchange min-notional) ──
    risk_pct_base: float = 0.01
    small_account_max_risk_pct: float = 0.015
    small_account_ceiling: float = 100.0  # bump only allowed below this equity

    # ── Portfolio caps (spec §C1 — the caps that make the math close) ──
    total_margin_cap_pct: float = 0.40    # total margin ≤ 40% of equity
    total_notional_cap_mult: float = 3.0  # total notional ≤ 3× equity
    max_risk_at_once_pct: float = 0.04    # total simultaneous risk ≤ 4% of equity

    # ── Leverage (spec §C1 — DERIVED, never used to size; hard ceiling 10×) ──
    max_leverage: int = 10
    min_leverage: int = 1

    # ── Concurrency ladder by balance band (spec §C2, §C5) ──
    # (upper_exclusive_balance, max_concurrent). The final band is unbounded.
    concurrency_ladder: Tuple[Tuple[float, int], ...] = (
        (100.0, 1),
        (250.0, 2),
        (500.0, 3),
        (1000.0, 3),
        (float('inf'), 4),
    )

    # ── Stop loss (spec §C2: structural + 1.5×ATR, floor 1%, ceiling 6%) ──
    stop_atr_mult: float = 1.5
    stop_floor_pct: float = 0.01          # never tighter than 1% (noise floor)
    stop_ceiling_pct: float = 0.06        # wider than 6% ⇒ skip the trade

    # ── Take profit / trade management (spec §C2) ──
    min_rr: float = 1.5                   # reject setups below this reward:risk
    target_rr: float = 2.0
    partial_close_fraction: float = 0.50  # close ~50% at the first partial
    partial_trigger_rr: float = 1.5       # first partial at ~1.5R
    trail_atr_mult: float = 2.0           # trailing distance in ATR (normal)
    trail_atr_mult_strong_trend: float = 2.5  # give a strong trend more room

    # ── Circuit breakers (spec §C2 — the 5/10/15/20 survival ladder) ──
    daily_loss_pct: float = 0.05          # halt + flatten for the day
    weekly_loss_pct: float = 0.10         # halt for the week
    monthly_loss_pct: float = 0.15        # halt + review
    max_drawdown_pct: float = 0.20        # full stop, manual restart
    daily_profit_pct: float = 0.04        # block new entries + tighten trails
    daily_giveback_frac: float = 0.40     # give back >40% of peak day PnL ⇒ protect
    consecutive_loss_cooldown: int = 4    # losses in a row ⇒ half-risk cooldown
    cooldown_risk_multiplier: float = 0.5

    # ── Directional heat (spec §A4, §C2: replaces count-based correlation) ──
    directional_heat_cap_pct: float = 0.03  # long-risk ≤3% AND short-risk ≤3%

    # ── Entry confluence gate (spec §A5, §C2: launch = confluence count; the
    #    calibrated 0–100 score and size tilt are deferred to a later phase) ──
    min_confluence: int = 4
    max_confluence: int = 6
    confidence_size_tilt_enabled: bool = False

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

    # ── Coin scanner / ranker (spec §6, §C2) ──
    min_quote_volume: float = 100_000_000.0  # 24h quote volume floor (liquidity)
    scanner_max_spread_pct: float = 0.0005   # spread ceiling for eligibility (0.05%)
    funding_extreme_abs: float = 0.003       # |funding| above this ⇒ ineligible
    min_atr_pct: float = 0.005               # below this ⇒ untradeable (dead) volatility
    max_atr_pct: float = 0.08                # above this ⇒ untradeable (un-stoppable)

    # ── Data freshness / execution safety (spec §C4) ──
    max_candle_age_seconds: int = 120        # stale market data ⇒ no new trades
    max_order_retries: int = 3
    order_retry_backoff_seconds: float = 0.5

    # ── Costs (for expectancy / net checks; spec §A12) ──
    taker_fee_pct: float = 0.0004         # round-trip applied as 2× at close


@dataclass(frozen=True)
class AccountLimits:
    """Resolved per-account limits for a specific live equity (spec §C5)."""

    tradeable: bool
    equity: float
    reason: str
    risk_pct: float = 0.0
    max_concurrent: int = 0
    max_risk_at_once_usd: float = 0.0
    total_notional_cap_usd: float = 0.0
    total_margin_cap_usd: float = 0.0
    margin_budget_per_trade_usd: float = 0.0


def _max_concurrent_for(equity: float, params: V4Params) -> int:
    """Concurrency for an equity from the ladder (spec §C2)."""
    for upper, concurrent in params.concurrency_ladder:
        if equity < upper:
            return concurrent
    return params.concurrency_ladder[-1][1]


def resolve_account_limits(
    equity: float, params: Optional[V4Params] = None
) -> AccountLimits:
    """Turn a live account equity into concrete, auto-scaled limits.

    This is the whole of "account auto-scaling": one function, no per-user config.
    Accounts below the hard floor are NOT tradeable (they must be managed to close
    only) — this protects the smallest, most fragile subscribers from min-notional
    over-risking (spec §A8).

    Args:
        equity: Live account equity in USDT.
        params: V4 parameters (defaults to the approved spec values).

    Returns:
        An AccountLimits describing exactly what this account may do.
    """
    p = params or V4Params()

    if not (equity > 0) or math.isnan(equity):
        return AccountLimits(
            tradeable=False, equity=float(equity or 0.0),
            reason='invalid_equity',
        )
    if equity < p.min_balance:
        return AccountLimits(
            tradeable=False, equity=equity,
            reason=f'below_min_balance ({equity:.2f} < {p.min_balance:.2f} USDT)',
        )

    max_concurrent = _max_concurrent_for(equity, p)
    # Guard: a tradeable account always has at least one slot.
    max_concurrent = max(1, max_concurrent)

    return AccountLimits(
        tradeable=True,
        equity=equity,
        reason='OK',
        risk_pct=p.risk_pct_base,
        max_concurrent=max_concurrent,
        max_risk_at_once_usd=equity * p.max_risk_at_once_pct,
        total_notional_cap_usd=equity * p.total_notional_cap_mult,
        total_margin_cap_usd=equity * p.total_margin_cap_pct,
        margin_budget_per_trade_usd=(equity * p.total_margin_cap_pct) / max_concurrent,
    )
