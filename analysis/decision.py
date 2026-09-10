"""Final Decision Layer — the single BUY / SELL / WAIT authority.

Every gate in the signal pipeline that can turn a would-be BUY/SELL into a
WAIT is expressed here as one ordered cascade, so there is exactly ONE place
in the codebase where "is this setup tradeable?" is answered.  The signal
generator, the API serializer and the frontend all consume the same
``FinalDecision`` object — none of them may reach past it to decide on their
own.

The cascade is deliberate:

  0. non-production strategy         → WAIT / experimental_strategy
                                              or custom_strategy
  1. no clear direction              → WAIT / no_direction
  2. hard conflicts (HTF veto etc.)  → WAIT / hard_conflict
  3. quality below the floor         → WAIT / quality_below_threshold
  4. confidence below the floor      → WAIT / confidence_below_threshold
  5. no valid stop level             → WAIT / no_valid_stop
  6. risk ceiling exceeded           → WAIT / risk_ceiling
  7. TP ladder cannot be built       → WAIT / insufficient_targets
  8. TP1 R:R below the floor         → WAIT / tp1_rr_below_minimum
  9. best R:R below the floor        → WAIT / rr_below_minimum
 10. else                            → BUY / SELL

Every reason carries a short machine-readable ``code`` (used by the API and
by tests) and a longer human ``message`` (used by the UI banner and by the
diagnostic panel).  R:R can only APPEAR in the cascade AFTER quality and
confidence have passed — a large R:R can never "compensate" for a weak setup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from analysis.confluence import ConfluenceResult
from analysis.scoring import (
    MIN_TRADEABLE_CONFIDENCE,
    MIN_TRADEABLE_QUALITY,
    Score,
)
from analysis.strategies import (
    blocked_reason_code,
    blocks_tradeable,
    identify_strategy,
)
from analysis.structure import BULLISH


BUY = 'BUY'
SELL = 'SELL'
WAIT = 'WAIT'


# Machine-readable rejection codes — stable so tests, the API, and the UI can
# all key off them.  Any new code MUST be added here first and referenced by
# name at the call site.
CODE_TRADEABLE = 'tradeable'
CODE_EXPERIMENTAL_STRATEGY = 'experimental_strategy'
CODE_CUSTOM_STRATEGY = 'custom_strategy'
CODE_MARKET_UNCLEAN = 'market_conditions_unsuitable'
CODE_HARD_CONFLICT = 'hard_conflict'
CODE_NO_DIRECTION = 'no_directional_consensus'
CODE_QUALITY_BELOW = 'quality_below_threshold'
CODE_CONFIDENCE_BELOW = 'confidence_below_threshold'
CODE_NO_STOP = 'no_valid_stop'
CODE_RISK_CEILING = 'risk_ceiling_exceeded'
CODE_TARGETS = 'insufficient_targets'
CODE_TP1_RR = 'tp1_rr_below_minimum'
CODE_MIN_RR = 'rr_below_minimum'


@dataclass(frozen=True)
class FinalDecision:
    """The one authoritative BUY / SELL / WAIT verdict.

    ``tradeable`` is the single boolean the API, the serializer and the UI
    check — never any combination of quality and confidence read
    independently.  When ``tradeable`` is False the ``direction`` field still
    records the WHICH SIDE the analysis leaned towards (so the UI can show
    "bias" for a WAIT) but the outward signal is always WAIT.
    """

    tradeable: bool
    signal: str                    # 'BUY' | 'SELL' | 'WAIT'
    direction_bias: Optional[str]  # 'long' | 'short' | None — the leaning side
    reason_code: str               # one of CODE_* above
    reason_message: str            # human-readable, ready to render in the UI
    secondary_reasons: List[str] = field(default_factory=list)


def _side_from_direction(direction: str) -> Optional[str]:
    """Trade-side helper: BULLISH → 'long', BEARISH → 'short'."""
    if direction == BULLISH:
        return 'long'
    if direction == 'bearish':
        return 'short'
    return None


def decide(
    *,
    market_blocking: Optional[str],
    confluence: ConfluenceResult,
    quality: Score,
    confidence: Score,
    min_quality: int = MIN_TRADEABLE_QUALITY,
    min_confidence: int = MIN_TRADEABLE_CONFIDENCE,
) -> FinalDecision:
    """Run the pre-risk portion of the cascade.

    This covers gates 1–4 in the cascade above — everything up to and
    including the confidence floor.  Risk gates (stop / R:R / targets) run
    inside ``analysis.generator`` because they need the concrete price
    levels; the generator constructs the final ``FinalDecision`` for those
    outcomes with :func:`decide_after_risk`.
    """
    bias = confluence.trade_side  # 'long' | 'short' | None

    # ── Gate 0: production status ──
    # Only a strategy someone has designed and audited may tell a user to take
    # a trade. Everything else — experimental strategies AND hand-built custom
    # configurations — is ANALYSED in full, so the scores, bias and diagnostic
    # are all still produced and research remains possible, but it may never
    # emit a tradeable BUY/SELL.
    #
    # The status is derived from the modules the ENGINE actually ran, so it
    # cannot be bypassed by naming one strategy and sending another's module
    # list, or by calling the API directly. The caller's DECLARED id is consulted
    # only to make the result more restrictive — declaring CUSTOM keeps a
    # hand-assembled selection out of production even when its modules happen to
    # equal a registered strategy's. It runs FIRST so no later gate can overturn
    # it.
    declared = getattr(confluence, 'declared_strategy_id', None)
    if blocks_tradeable(confluence.enabled_modules, declared):
        strategy = identify_strategy(confluence.enabled_modules, declared)
        code = blocked_reason_code(confluence.enabled_modules, declared)
        if code == CODE_CUSTOM_STRATEGY:
            message = (
                'Custom is a manual research configuration, not a validated '
                'strategy. The analysis runs in full, but a configuration '
                'nobody has validated does not produce tradeable signals.'
            )
        else:
            message = (
                f'{strategy.name} is an experimental strategy — research and '
                'preview only. It has not been backtested or validated, so it '
                'does not produce tradeable signals.'
            )
        return FinalDecision(
            tradeable=False, signal=WAIT, direction_bias=bias,
            reason_code=code, reason_message=message,
        )

    if market_blocking:
        return FinalDecision(
            tradeable=False, signal=WAIT, direction_bias=bias,
            reason_code=CODE_MARKET_UNCLEAN,
            reason_message=f'Market conditions unsuitable — {market_blocking}.',
        )

    if confluence.hard_conflicts:
        primary = confluence.hard_conflicts[0]
        return FinalDecision(
            tradeable=False, signal=WAIT, direction_bias=bias,
            reason_code=CODE_HARD_CONFLICT,
            reason_message=(
                f'Conflicting signals — {primary}.'
            ),
            secondary_reasons=list(confluence.hard_conflicts[1:]),
        )

    if not confluence.has_direction:
        return FinalDecision(
            tradeable=False, signal=WAIT, direction_bias=bias,
            reason_code=CODE_NO_DIRECTION,
            reason_message=(
                confluence.reason
                or 'No directional consensus across the enabled modules.'
            ),
        )

    if quality.value < min_quality:
        return FinalDecision(
            tradeable=False, signal=WAIT, direction_bias=bias,
            reason_code=CODE_QUALITY_BELOW,
            reason_message=(
                f'Setup quality {quality.value}/100 ({quality.grade}) is below '
                f'the {min_quality}/100 tradeable threshold.'
            ),
        )

    if confidence.value < min_confidence:
        return FinalDecision(
            tradeable=False, signal=WAIT, direction_bias=bias,
            reason_code=CODE_CONFIDENCE_BELOW,
            reason_message=(
                f'Confidence {confidence.value}/100 ({confidence.grade}) is '
                f'below the {min_confidence}/100 tradeable threshold.'
            ),
        )

    # All pre-risk gates cleared — the setup is ELIGIBLE for a signal.  The
    # generator still owns the risk gates and will call
    # :func:`build_tradeable` / :func:`reject` accordingly.
    return FinalDecision(
        tradeable=True,
        signal=BUY if bias == 'long' else SELL,
        direction_bias=bias,
        reason_code=CODE_TRADEABLE,
        reason_message='All decision gates passed.',
    )


def reject(bias: Optional[str], code: str, message: str) -> FinalDecision:
    """Build a WAIT decision produced by a risk-gate rejection."""
    return FinalDecision(
        tradeable=False, signal=WAIT, direction_bias=bias,
        reason_code=code, reason_message=message,
    )


def approve(bias: str) -> FinalDecision:
    """Build a BUY / SELL decision after every risk gate has passed."""
    signal = BUY if bias == 'long' else SELL
    return FinalDecision(
        tradeable=True, signal=signal, direction_bias=bias,
        reason_code=CODE_TRADEABLE,
        reason_message='All decision gates passed.',
    )
