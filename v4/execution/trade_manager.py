"""V4 Trade Manager (spec §C2, §C4).

Manages ALL open positions for a single account on one tick, under the authority
of the circuit breakers:

  • must_flatten  → force-close every position and cancel its resting stop.
  • allow_new=False (profit lock / giveback / cooldown) → do not open new (opening
    is orchestrated elsewhere); still manage existing positions to protect them.
  • otherwise     → advance each position's trade_math state machine.

Reconciliation runs first so a position whose exchange leg vanished is finalized
before management, exactly as the V1 engine does — carried into V4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from v4.execution.position_manager import V4Position, V4PositionManager
from v4.params import V4Params
from v4.portfolio import CircuitDecision


@dataclass
class TickReport:
    flattened: bool = False
    managed: int = 0
    closed: int = 0
    reconciled: int = 0
    actions: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class V4TradeManager:
    def __init__(
        self, position_manager: V4PositionManager, params: Optional[V4Params] = None,
    ) -> None:
        self.pm = position_manager
        self.params = params or V4Params()

    def manage_tick(
        self,
        positions: List[V4Position],
        price_map: Dict[str, float],
        atr_map: Dict[str, float],
        circuit: CircuitDecision,
        live_keys: Optional[set] = None,
    ) -> TickReport:
        """Manage all positions for one account on one tick."""
        report = TickReport()
        live_keys = live_keys if live_keys is not None else {
            (p.symbol, p.side) for p in positions if not p.closed
        }

        # ── Reconcile vanished exchange legs first ──
        for pos in positions:
            if pos.closed:
                continue
            if self.pm.reconcile(pos, live_keys):
                report.reconciled += 1

        active = [p for p in positions if not p.closed]

        # ── Circuit: hard flatten outranks everything ──
        if circuit.must_flatten:
            for pos in active:
                out = self.pm.force_close(pos, circuit.level)
                report.actions.extend(out.actions)
                report.errors.extend(out.errors)
                report.closed += 1
            report.flattened = True
            return report

        # ── Manage each open position ──
        for pos in active:
            price = price_map.get(pos.symbol)
            atr = atr_map.get(pos.symbol)
            if not price or atr is None:
                report.errors.append(f'no_market_data:{pos.symbol}')
                continue
            out = self.pm.manage(pos, price, atr)
            report.managed += 1
            report.actions.extend(out.actions)
            report.errors.extend(out.errors)
            if pos.closed:
                report.closed += 1
        return report
