"""Signal Validation Engine.

Reviews the signal the Analysis Engine already produced and checks, module by
module, whether each required confirmation is present. It NEVER generates a new
signal and NEVER changes entry, stop, targets, or scores — it only reports which
confirmations exist for the direction the engine chose.

The Validation Score is a separate number from Quality and Confidence: Quality
asks "how good is this setup?", Confidence asks "how sure is the engine?", and
Validation asks the narrower question "how many of the confirmations we require
are actually present and aligned?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from analysis.intelligence.context import SignalContext
from analysis.modules import (
    MODULE_ORDER,
    MODULE_WEIGHTS,
    NEUTRAL_VOTE,
    STRENGTH_MODULES,
)

# Human labels for each module in the checklist.
MODULE_LABELS = {
    'trend': 'Trend',
    'structure': 'Market Structure',
    'elliott': 'Elliott Wave',
    'fibonacci': 'Fibonacci',
    'volume': 'Volume',
    'rsi': 'RSI',
    'atr': 'ATR / Volatility',
    'support_resistance': 'Support & Resistance',
    'order_block': 'Order Block',
    'fvg': 'Fair Value Gap',
    'liquidity': 'Liquidity',
    'vwap': 'VWAP',
    'macd': 'MACD',
    'adx': 'ADX',
    'pattern': 'Pattern',
}

CONFIRMED = 'confirmed'
NEUTRAL = 'neutral'
AGAINST = 'against'

# A strength module (ATR/ADX) counts as confirmed when its reading is this
# suitable or better — it has no side, so it confirms by being favourable.
STRENGTH_CONFIRM_THRESHOLD = 0.5

# Validation status bands.
STATUS_BANDS = (
    (85, 'Excellent Validation'),
    (70, 'Strong Validation'),
    (50, 'Moderate Validation'),
    (0, 'Weak Validation'),
)


@dataclass(frozen=True)
class ValidationCheck:
    """One module's line in the validation checklist."""

    module: str
    label: str
    status: str            # 'confirmed' | 'neutral' | 'against'
    detail: str
    weight: int


@dataclass
class ValidationResult:
    """The full validation review of one signal."""

    score: int             # 0–100
    status: str            # 'Excellent Validation' … 'Weak Validation'
    confirmed: int
    against: int
    neutral: int
    total: int
    checks: List[ValidationCheck] = field(default_factory=list)

    @property
    def confirmed_modules(self) -> List[str]:
        return [c.label for c in self.checks if c.status == CONFIRMED]

    @property
    def against_modules(self) -> List[str]:
        return [c.label for c in self.checks if c.status == AGAINST]


def validate_signal(ctx: SignalContext) -> ValidationResult:
    """Review every module's confirmation of the signal's direction.

    Args:
        ctx: The read-only signal context.

    Returns:
        A ValidationResult. The score is the weighted share of module weight that
        is aligned with the direction — a confirmation is worth its module's full
        weight, a neutral module half, an opposing module nothing.
    """
    side = ctx.side
    checks: List[ValidationCheck] = []
    aligned_weight = 0.0
    total_weight = 0.0

    for module in MODULE_ORDER:
        vote = ctx.confluence.vote(module)
        weight = MODULE_WEIGHTS[module]
        total_weight += weight
        label = MODULE_LABELS.get(module, module)

        if vote is None:
            checks.append(ValidationCheck(module, label, NEUTRAL, 'not evaluated', weight))
            continue

        if module in STRENGTH_MODULES:
            # ATR / ADX confirm by being suitable, not by taking a side.
            if vote.strength >= STRENGTH_CONFIRM_THRESHOLD:
                status = CONFIRMED
                aligned_weight += weight
            else:
                status = NEUTRAL
                aligned_weight += weight * 0.5
        elif side is not None and vote.agrees_with(side):
            status = CONFIRMED
            aligned_weight += weight
        elif side is not None and vote.opposes(side):
            status = AGAINST
        elif vote.direction == NEUTRAL_VOTE:
            status = NEUTRAL
            aligned_weight += weight * 0.5
        else:
            # Directional module leaning the other way when we have no side.
            status = NEUTRAL
            aligned_weight += weight * 0.5

        checks.append(ValidationCheck(module, label, status, vote.detail, weight))

    score = int(round(100 * aligned_weight / total_weight)) if total_weight else 0
    score = max(0, min(100, score))

    return ValidationResult(
        score=score,
        status=_status_for(score),
        confirmed=sum(1 for c in checks if c.status == CONFIRMED),
        against=sum(1 for c in checks if c.status == AGAINST),
        neutral=sum(1 for c in checks if c.status == NEUTRAL),
        total=len(checks),
        checks=checks,
    )


def _status_for(score: int) -> str:
    for threshold, label in STATUS_BANDS:
        if score >= threshold:
            return label
    return 'Weak Validation'
