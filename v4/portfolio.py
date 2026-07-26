"""Directional Heat + Circuit Breakers (spec §A2, §A3, §A4, §C2).

Pure evaluators over supplied account state — no DB, no exchange. The portfolio
layer is where survival is enforced:

  • Directional Heat: caps total long-risk and total short-risk independently, so
    a book of correlated alts cannot become one oversized directional bet
    (replaces V1's unworkable count-based "correlation protection", spec §A4).

  • Circuit Breakers: the 5/10/15/20 survival ladder plus a runner-safe profit
    ring-fence and a consecutive-loss cooldown. Profit protection NEVER
    force-closes a healthy runner — it only blocks new risk and tightens trails
    (spec §A3). Daily loss is a hard cap that flattens (spec §A2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from v4.params import V4Params


@dataclass(frozen=True)
class HeatDecision:
    allowed: bool
    reason: str
    projected_long_risk: float
    projected_short_risk: float
    cap: float


def check_directional_heat(
    side: str,
    new_risk_usd: float,
    open_long_risk_usd: float,
    open_short_risk_usd: float,
    equity: float,
    params: Optional[V4Params] = None,
) -> HeatDecision:
    """Allow a new position only if its side's total risk stays within the cap."""
    p = params or V4Params()
    side = (side or '').lower()
    cap = equity * p.directional_heat_cap_pct
    proj_long = open_long_risk_usd + (new_risk_usd if side == 'long' else 0.0)
    proj_short = open_short_risk_usd + (new_risk_usd if side == 'short' else 0.0)

    if side not in ('long', 'short'):
        return HeatDecision(False, f'invalid_side ({side!r})', proj_long, proj_short, cap)

    projected = proj_long if side == 'long' else proj_short
    if projected > cap + 1e-9:
        return HeatDecision(
            False,
            f'directional_heat_cap ({side} risk {projected:.4f} > cap {cap:.4f})',
            proj_long, proj_short, cap,
        )
    return HeatDecision(True, 'OK', proj_long, proj_short, cap)


@dataclass(frozen=True)
class CircuitInputs:
    """Snapshot of account performance state, all PnL as fractions of equity."""

    daily_pnl_pct: float
    weekly_pnl_pct: float
    monthly_pnl_pct: float
    drawdown_pct: float            # peak-to-trough, POSITIVE number (0.0 = at peak)
    consecutive_losses: int
    day_peak_pnl_pct: float = 0.0  # highest daily_pnl_pct reached today


@dataclass(frozen=True)
class CircuitDecision:
    allow_new_entries: bool
    must_flatten: bool             # hard close ALL open positions now
    tighten_trails: bool           # protect open winners without force-closing
    halted: bool                   # full stop, manual restart required
    risk_multiplier: float         # 1.0 normal, 0.5 during a loss-cooldown
    level: str
    reason: str


def evaluate_circuit_breakers(
    inp: CircuitInputs, params: Optional[V4Params] = None
) -> CircuitDecision:
    """Evaluate the survival ladder; return the STRICTEST applicable decision.

    Severity order (spec §C2): max-drawdown > monthly > weekly > daily-loss >
    daily-profit ring-fence / giveback. A consecutive-loss cooldown halves risk
    but does not by itself halt trading.
    """
    p = params or V4Params()

    # Consecutive-loss cooldown (applies as a risk multiplier regardless of level).
    risk_mult = (
        p.cooldown_risk_multiplier
        if inp.consecutive_losses >= p.consecutive_loss_cooldown
        else 1.0
    )

    # 1) Max drawdown — full stop, manual restart (does not auto-flatten; §C2).
    if inp.drawdown_pct >= p.max_drawdown_pct:
        return CircuitDecision(
            False, False, True, True, risk_mult, 'max_drawdown',
            f'drawdown {inp.drawdown_pct:.4f} ≥ {p.max_drawdown_pct:.4f} — full stop, '
            f'manual restart required',
        )

    # 2) Monthly loss — halt + review.
    if inp.monthly_pnl_pct <= -p.monthly_loss_pct:
        return CircuitDecision(
            False, False, True, False, risk_mult, 'monthly_loss',
            f'monthly PnL {inp.monthly_pnl_pct:.4f} ≤ -{p.monthly_loss_pct:.4f} — halt + review',
        )

    # 3) Weekly loss — halt for the week.
    if inp.weekly_pnl_pct <= -p.weekly_loss_pct:
        return CircuitDecision(
            False, False, True, False, risk_mult, 'weekly_loss',
            f'weekly PnL {inp.weekly_pnl_pct:.4f} ≤ -{p.weekly_loss_pct:.4f} — halt for the week',
        )

    # 4) Daily loss — HARD cap: halt + flatten (spec §A2).
    if inp.daily_pnl_pct <= -p.daily_loss_pct:
        return CircuitDecision(
            False, True, True, False, risk_mult, 'daily_loss',
            f'daily PnL {inp.daily_pnl_pct:.4f} ≤ -{p.daily_loss_pct:.4f} — halt + flatten',
        )

    # 5) Daily profit ring-fence + giveback — block new + tighten, NEVER flatten
    #    a healthy runner (spec §A3).
    giveback_hit = (
        inp.day_peak_pnl_pct > 0
        and (inp.day_peak_pnl_pct - inp.daily_pnl_pct)
        > p.daily_giveback_frac * inp.day_peak_pnl_pct
    )
    if inp.daily_pnl_pct >= p.daily_profit_pct:
        return CircuitDecision(
            False, False, True, False, risk_mult, 'daily_profit_lock',
            f'daily PnL {inp.daily_pnl_pct:.4f} ≥ {p.daily_profit_pct:.4f} — block new '
            f'entries + tighten trails (runners protected, not force-closed)',
        )
    if giveback_hit:
        return CircuitDecision(
            False, False, True, False, risk_mult, 'daily_giveback',
            f'gave back >{p.daily_giveback_frac:.0%} of peak day PnL '
            f'({inp.day_peak_pnl_pct:.4f}→{inp.daily_pnl_pct:.4f}) — protect gains',
        )

    # Normal operation (possibly at half-risk during a loss cooldown).
    if risk_mult < 1.0:
        return CircuitDecision(
            True, False, False, False, risk_mult, 'loss_cooldown',
            f'{inp.consecutive_losses} consecutive losses — trading at '
            f'{risk_mult:.0%} risk',
        )
    return CircuitDecision(True, False, False, False, 1.0, 'normal', 'OK')
