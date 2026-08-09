"""Zentry V4 — market-analysis stack.

A self-contained, pure-computation strategy stack: no exchange calls, no
database, no side effects — so it is fully unit-testable and reusable by the
signal generator built in Phase 1.

  • params      — analysis parameters (regime thresholds, stop/target math, gates)
  • indicators  — ADX / DI (Wilder) and helpers not present in signals.indicators
  • regime      — Regime Detection Engine (direction + volatility)
  • trade_math  — structural + ATR stop placement and R-multiple target levels
  • engines     — trend / breakout entry-candidate engines with confluence scoring

The execution half of this package (orchestrator, order/position/trade managers,
sizing, portfolio heat, kill switch, recovery, safety, account state, analytics)
was removed in the Phase 0 conversion: it existed only to place and manage live
orders.
"""

from v4.params import V4Params

__all__ = ['V4Params']
