"""Trade Intelligence Layer (Phase 3).

A read-only post-processing layer over a completed `AnalysisResult`. It reviews,
rates, and explains the signal the Analysis Engine already produced — it never
generates its own BUY/SELL/WAIT and never changes entry, stop, targets, quality,
or confidence. The technical engine always has final authority.

Reuses Phase 1 and Phase 2 analysis wholesale: every engine here reads from the
one `SignalContext` snapshot, so no indicator is recomputed.

  validation   per-module confirmation checklist + 0–100 score + status
  health       1–5 star overall rating
  trade_guide  dynamic step-by-step management plan from the real levels
  lifecycle    status, expiration, and the execution timeline
  risk         Low/Medium/High advisory with reasons from the analysis
  invalidation the exact conditions that void the trade
  narrative    summary + beginner + professional explanations
"""

from dataclasses import dataclass

from analysis.intelligence.context import SignalContext
from analysis.intelligence.health import HealthResult, rate_health
from analysis.intelligence.invalidation import Invalidation, build_invalidation
from analysis.intelligence.lifecycle import Lifecycle, build_lifecycle
from analysis.intelligence.narrative import Narrative, build_narrative
from analysis.intelligence.risk import RiskAdvisory, assess_risk
from analysis.intelligence.trade_guide import TradeGuide, build_trade_guide
from analysis.intelligence.validation import ValidationResult, validate_signal


@dataclass
class TradeIntelligence:
    """Everything the intelligence layer produced for one signal."""

    validation: ValidationResult
    health: HealthResult
    trade_guide: TradeGuide
    lifecycle: Lifecycle
    risk: RiskAdvisory
    invalidation: Invalidation
    narrative: Narrative


def build_intelligence(result) -> TradeIntelligence:
    """Assemble the full intelligence layer from a completed AnalysisResult.

    Pure and read-only: it recomputes no market data and mutates nothing on the
    result it is given.
    """
    ctx = SignalContext.from_result(result)
    validation = validate_signal(ctx)
    return TradeIntelligence(
        validation=validation,
        health=rate_health(ctx, validation),
        trade_guide=build_trade_guide(ctx),
        lifecycle=build_lifecycle(ctx),
        risk=assess_risk(ctx),
        invalidation=build_invalidation(ctx),
        narrative=build_narrative(ctx),
    )


__all__ = [
    'HealthResult',
    'Invalidation',
    'Lifecycle',
    'Narrative',
    'RiskAdvisory',
    'SignalContext',
    'TradeGuide',
    'TradeIntelligence',
    'ValidationResult',
    'assess_risk',
    'build_intelligence',
    'build_invalidation',
    'build_lifecycle',
    'build_narrative',
    'build_trade_guide',
    'rate_health',
    'validate_signal',
]
