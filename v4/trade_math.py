"""Stop Loss and Take Profit level math (spec §C2).

Pure functions that turn an entry, a side, an ATR reading, and an optional
structural level into the concrete price levels a signal reports: a stop, and
the R-multiple targets measured from it.

The live trade-management state machine that used to live here (`TradeState`,
`manage`, `TradeAction` — partial fills, break-even moves, trailing stops) was
removed in the Phase 0 conversion: it only had meaning while the system held an
open position it was managing automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from v4.params import V4Params


@dataclass(frozen=True)
class StopResult:
    valid: bool
    reason: str
    stop_price: float = 0.0
    stop_distance: float = 0.0
    stop_pct: float = 0.0


def compute_stop(
    entry: float,
    side: str,
    atr: float,
    structural_level: Optional[float] = None,
    params: Optional[V4Params] = None,
) -> StopResult:
    """Hybrid stop: structural anchor + ATR buffer, floor/ceiling applied (spec §C2).

    If a structural level (swing/range edge) is supplied, the stop is placed just
    beyond it by an ATR buffer; otherwise it falls back to an ATR-multiple stop
    from entry. Distance is clamped UP to the 1% floor; a stop wider than the
    ceiling returns valid=False so the caller REJECTS the setup.
    """
    p = params or V4Params()
    side = (side or '').lower()
    if entry <= 0 or atr < 0 or side not in ('long', 'short'):
        return StopResult(False, 'invalid_inputs')

    buffer = atr * p.stop_atr_mult
    if side == 'long':
        anchor = structural_level if structural_level is not None else entry
        stop = anchor - buffer if structural_level is not None else entry - buffer
    else:
        anchor = structural_level if structural_level is not None else entry
        stop = anchor + buffer if structural_level is not None else entry + buffer

    # Ensure the stop is on the correct side of entry even with an odd structural
    # level (e.g. a structural level already through price).
    if side == 'long' and stop >= entry:
        stop = entry - buffer
    if side == 'short' and stop <= entry:
        stop = entry + buffer

    distance = abs(entry - stop)
    pct = distance / entry if entry > 0 else 0.0

    if pct > p.stop_ceiling_pct + 1e-12:
        return StopResult(
            False, f'stop_too_wide ({pct:.4f} > {p.stop_ceiling_pct:.4f})',
            stop_price=stop, stop_distance=distance, stop_pct=pct,
        )
    if pct < p.stop_floor_pct:
        distance = p.stop_floor_pct * entry
        stop = entry - distance if side == 'long' else entry + distance
        pct = p.stop_floor_pct

    return StopResult(True, 'OK', stop_price=stop, stop_distance=distance, stop_pct=pct)


def take_profit_levels(
    entry: float, stop_price: float, side: str, params: Optional[V4Params] = None
) -> dict:
    """Compute the R value and the R-multiple target prices (spec §C2).

    Returns {'r_value', 'partial_price', 'target_price', 'min_rr_price'}. R is the
    per-unit risk (|entry − stop|); prices are entry ± (rr × R) with the correct
    sign for the side.
    """
    p = params or V4Params()
    side = (side or '').lower()
    r = abs(entry - stop_price)
    sign = 1.0 if side == 'long' else -1.0
    return {
        'r_value': r,
        'partial_price': entry + sign * p.partial_trigger_rr * r,
        'target_price': entry + sign * p.target_rr * r,
        'min_rr_price': entry + sign * p.min_rr * r,
    }
