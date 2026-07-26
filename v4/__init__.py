"""ZenGrid Version 4 — parallel, feature-flagged trading stack.

This package implements the APPROVED V4 Final Specification as a self-contained
strategy stack that runs ALONGSIDE the existing (V1) mean-reversion basket engine
without modifying or breaking it. Nothing here is imported by the live loop until
the V4 feature flag is wired in (Phase 6), so adding this package is non-breaking
by construction.

Phase 1 (this module set) is the pure decision & risk-math core — no exchange and
no database dependencies — so it is fully unit-testable:

  • params      — finalized V4 parameters + per-balance auto-scaling
  • indicators  — ADX / DI (Wilder) and helpers not present in signals.indicators
  • regime      — Regime Detection Engine (direction + volatility + systemic gate)
  • sizing      — Position Sizing + Dynamic Leverage (the mathematically-closed
                  risk stack from spec §C1) with full pre-order validation
  • trade_math  — Stop Loss, Take Profit, Partial TP, Break-even, Trailing Stop
  • portfolio   — Directional Heat + Daily/Weekly/Monthly/Drawdown circuit breakers
"""

from v4.params import (
    AccountLimits,
    V4Params,
    resolve_account_limits,
)

# Convenience re-exports for the assembled system (Phases 2–6).
from v4.kill_switch import KillSwitch
from v4.orchestrator import AccountContext, MarketData, V4Orchestrator

__all__ = [
    'V4Params',
    'AccountLimits',
    'resolve_account_limits',
    'KillSwitch',
    'V4Orchestrator',
    'MarketData',
    'AccountContext',
]
