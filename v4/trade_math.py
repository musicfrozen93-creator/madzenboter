"""Stop Loss, Take Profit, Partial TP, Break-even, and Trailing Stop (spec §C2).

Pure, serializable trade-management math. `TradeState` is a plain dataclass so it
can be persisted to the DB and rebuilt on restart (spec §C4 restart recovery).
`manage` is a deterministic state machine: given the current price and ATR it
returns the exact actions to take (take partial, move stop, close) and the updated
state. It NEVER force-closes a healthy runner — profit protection lives in the
portfolio layer and only tightens/blocks (spec §A3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

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
    ceiling returns valid=False so the caller SKIPS the trade (never widens size).
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
    """Compute R value, the partial-TP price, and the target price (spec §C2).

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


@dataclass
class TradeState:
    """Serializable state of one managed V4 position."""

    side: str
    entry: float
    initial_stop: float
    current_stop: float
    quantity: float
    remaining_qty: float
    partial_price: float
    r_value: float
    partial_done: bool = False
    breakeven_set: bool = False
    strong_trend: bool = False
    closed: bool = False

    @classmethod
    def open(
        cls, side: str, entry: float, stop_price: float, quantity: float,
        params: Optional[V4Params] = None, strong_trend: bool = False,
    ) -> 'TradeState':
        p = params or V4Params()
        levels = take_profit_levels(entry, stop_price, side, p)
        return cls(
            side=(side or '').lower(), entry=entry, initial_stop=stop_price,
            current_stop=stop_price, quantity=quantity, remaining_qty=quantity,
            partial_price=levels['partial_price'], r_value=levels['r_value'],
            strong_trend=strong_trend,
        )


@dataclass(frozen=True)
class TradeAction:
    type: str            # 'take_partial' | 'move_stop' | 'close'
    reason: str
    price: float = 0.0
    fraction: float = 0.0
    quantity: float = 0.0
    stop: float = 0.0


def manage(
    state: TradeState, price: float, atr: float, params: Optional[V4Params] = None
) -> List[TradeAction]:
    """Advance one managed position by one price observation (spec §C2).

    Sequence per observation:
      1. If the stop is hit → close the remainder (backup to the resting exchange
         stop; both agree).
      2. Else if the first partial level is reached and not yet taken → take the
         partial and move the stop to break-even.
      3. Else if the partial is done → trail the stop (tighten-only) by ATR.

    Mutates `state` in place and returns the ordered actions taken.
    """
    p = params or V4Params()
    actions: List[TradeAction] = []
    if state.closed or state.remaining_qty <= 0 or price <= 0:
        return actions

    long = state.side == 'long'

    # 1) Stop hit (authoritative software backup for the resting exchange stop).
    if (long and price <= state.current_stop) or (not long and price >= state.current_stop):
        actions.append(TradeAction(
            'close', 'stop_hit', price=price, quantity=state.remaining_qty,
            stop=state.current_stop,
        ))
        state.remaining_qty = 0.0
        state.closed = True
        return actions

    # 2) First partial + move to break-even.
    if not state.partial_done:
        reached = price >= state.partial_price if long else price <= state.partial_price
        if reached:
            close_qty = state.remaining_qty * p.partial_close_fraction
            actions.append(TradeAction(
                'take_partial', 'partial_tp', price=price,
                fraction=p.partial_close_fraction, quantity=close_qty,
            ))
            state.remaining_qty -= close_qty
            state.partial_done = True
            # Move stop to break-even (entry) — tighten-only guarded below.
            if (long and state.entry > state.current_stop) or (
                not long and state.entry < state.current_stop
            ):
                state.current_stop = state.entry
            state.breakeven_set = True
            actions.append(TradeAction(
                'move_stop', 'breakeven', stop=state.current_stop,
            ))
            return actions
        return actions

    # 3) Trailing (only after the partial / break-even), tighten-only.
    mult = p.trail_atr_mult_strong_trend if state.strong_trend else p.trail_atr_mult
    trail = price - atr * mult if long else price + atr * mult
    new_stop = max(state.current_stop, trail) if long else min(state.current_stop, trail)
    if new_stop != state.current_stop:
        state.current_stop = new_stop
        actions.append(TradeAction('move_stop', 'trail', stop=new_stop))
    return actions
