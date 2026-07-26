"""Safety Validation — the final pre-order gate (spec §4, §C4).

A single, ordered checklist that must pass before any order is ever placed. It
composes the outputs of the regime engine, the confluence gate, the stop
calculation, the sizer, the directional-heat check, and the circuit breakers, and
adds operational guards (stale data, duplicate). Pure over supplied inputs so it
is fully unit-testable and cannot itself touch the exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from v4.engines.base import V4Signal
from v4.params import V4Params
from v4.portfolio import CircuitDecision, HeatDecision
from v4.regime import RegimeState
from v4.sizing import SizingPlan
from v4.trade_math import StopResult


@dataclass(frozen=True)
class SafetyResult:
    approved: bool
    reason: str
    stage: str   # which check produced the verdict


def validate_entry(
    regime: RegimeState,
    signal: Optional[V4Signal],
    stop: Optional[StopResult],
    plan: Optional[SizingPlan],
    heat: Optional[HeatDecision],
    circuit: Optional[CircuitDecision],
    candle_age_seconds: float,
    is_duplicate: bool = False,
    params: Optional[V4Params] = None,
) -> SafetyResult:
    """Run the ordered safety checklist; return the first failure or APPROVED.

    Order (most systemic → most specific):
      1. circuit breakers permit new entries
      2. market data is fresh
      3. regime is armed
      4. an engine produced a signal that clears the confluence gate
      5. the stop is valid (structural, within the ceiling)
      6. sizing is suitable (all caps + min-notional satisfied)
      7. directional heat permits the new risk
      8. not a duplicate of an in-flight/open order
    """
    p = params or V4Params()

    if circuit is None or not circuit.allow_new_entries:
        return SafetyResult(False, circuit.reason if circuit else 'no_circuit_state', 'circuit')

    if candle_age_seconds > p.max_candle_age_seconds:
        return SafetyResult(
            False,
            f'stale_data (candle age {candle_age_seconds:.0f}s > {p.max_candle_age_seconds}s)',
            'freshness',
        )

    if regime is None or not regime.tradeable or regime.armed_engine is None:
        return SafetyResult(False, regime.reason if regime else 'no_regime', 'regime')

    if signal is None:
        return SafetyResult(False, 'no_signal', 'signal')
    if signal.confluence < p.min_confluence:
        return SafetyResult(
            False, f'confluence_below_gate ({signal.confluence}/{p.min_confluence})', 'confluence'
        )

    if stop is None or not stop.valid:
        return SafetyResult(False, stop.reason if stop else 'no_stop', 'stop')

    if plan is None or not plan.suitable:
        return SafetyResult(False, plan.reason if plan else 'no_plan', 'sizing')

    if heat is None or not heat.allowed:
        return SafetyResult(False, heat.reason if heat else 'no_heat_state', 'heat')

    if is_duplicate:
        return SafetyResult(False, 'duplicate_order_guard', 'duplicate')

    return SafetyResult(True, 'OK', 'approved')
