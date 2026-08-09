"""Elliott Wave — deterministic, rule-based wave counting.

Elliott Wave is inherently ambiguous: the same swing sequence usually supports
more than one valid count. This engine embraces that instead of hiding it. It
enumerates every count the recent pivots can support, scores each against
Elliott's three hard rules and its well-known guidelines, and returns the two
best as a PRIMARY and an ALTERNATIVE with probabilities that sum to 100.

The three hard rules (violating one does not delete a count, it heavily
penalises it — a violated count can still be the least-bad reading):

  R1  Wave 2 never retraces more than 100% of wave 1.
  R2  Wave 3 is never the shortest of waves 1, 3 and 5.
  R3  Wave 4 never enters wave 1's price territory.

Guidelines are probabilistic and only add score:

  G1  Wave 2 typically retraces 50–61.8% of wave 1.
  G2  Wave 3 is commonly 1.618× wave 1 and is usually the extended wave.
  G3  Wave 4 typically retraces 23.6–50% of wave 3.
  G4  Wave 5 commonly equals wave 1, or 0.618× wave 3.
  G5  In an A-B-C zigzag, B retraces 38.2–78.6% of A and C ≈ A or 1.618× A.

No count is ever reported as certain: confidence is capped below 100.

Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from analysis.structure import Swing

IMPULSE = 'impulse'
CORRECTIVE = 'corrective'

# Confidence is deliberately capped — an Elliott count is never a certainty.
MAX_CONFIDENCE = 92.0
MIN_CONFIDENCE = 8.0

# A hard-rule violation multiplies a hypothesis score by this factor.
RULE_VIOLATION_PENALTY = 0.2

# Base score every structurally coherent hypothesis starts from.
BASE_SCORE = 40.0

# Evidence bonus per pivot used — a 5-pivot count is better evidenced than a
# 2-pivot one, so deeper counts win ties.
PIVOT_EVIDENCE_BONUS = 4.0


@dataclass(frozen=True)
class WaveCount:
    """One candidate wave count."""

    label: str                       # 'Wave 3', 'Wave C', …
    degree: str                      # 'impulse' | 'corrective'
    direction: str                   # 'bullish' | 'bearish'
    confidence: float                # 0–100, normalised across candidates
    completion_pct: float            # 0–100 progress through the current wave
    projected_target: Optional[float]
    wave_start: float
    pivots_used: int
    rules_passed: List[str] = field(default_factory=list)
    rules_violated: List[str] = field(default_factory=list)
    guidelines_met: List[str] = field(default_factory=list)
    reason: str = ''

    @property
    def is_valid(self) -> bool:
        """True when no hard Elliott rule is broken."""
        return not self.rules_violated

    def supports(self, direction: str) -> bool:
        """True when the count's direction agrees with a trade direction."""
        return self.direction == ('bullish' if direction == 'long' else 'bearish')


@dataclass
class ElliottState:
    """The wave read for one timeframe."""

    primary: Optional[WaveCount] = None
    alternative: Optional[WaveCount] = None
    direction: str = 'neutral'
    confidence: float = 0.0
    pivots_available: int = 0
    reason: str = 'insufficient_swings'

    @property
    def valid(self) -> bool:
        return self.primary is not None

    @property
    def label(self) -> str:
        return self.primary.label if self.primary else 'Undetermined'

    @property
    def alternative_label(self) -> str:
        return self.alternative.label if self.alternative else 'None'

    def supports(self, direction: str) -> bool:
        return self.primary is not None and self.primary.supports(direction)


# ─────────────────────────────────────────────
# Internal hypothesis representation
# ─────────────────────────────────────────────

@dataclass
class _Hypothesis:
    label: str
    degree: str
    direction: str
    score: float
    completion_pct: float
    projected_target: Optional[float]
    wave_start: float
    pivots_used: int
    rules_passed: List[str]
    rules_violated: List[str]
    guidelines_met: List[str]
    reason: str


def analyze_elliott(swings: List[Swing], price: float) -> ElliottState:
    """Enumerate, score, and rank Elliott counts for the current pivot sequence.

    Args:
        swings: Alternating confirmed swings, oldest first.
        price: Current price — the tentative end of the wave in progress.

    Returns:
        An ElliottState with the two best counts. When fewer than two pivots are
        confirmed, `valid` is False and nothing is guessed.
    """
    if len(swings) < 2 or price <= 0:
        return ElliottState(pivots_available=len(swings))

    hypotheses: List[_Hypothesis] = []
    # Try progressively deeper pivot windows. Each window yields whichever
    # impulse count its orientation and depth can support.
    for depth in range(2, min(len(swings), 6) + 1):
        hypotheses.extend(_impulse_hypotheses(swings[-depth:], price))

    # The corrective reading only ever examines the last three pivots, so it is
    # built ONCE. Generating it per depth would produce identical duplicates and
    # hand back an "alternative" that is really the same count twice.
    hypotheses.extend(_corrective_hypotheses(swings, price))

    hypotheses = _deduplicate(hypotheses)
    if not hypotheses:
        return ElliottState(
            pivots_available=len(swings), reason='no coherent count available'
        )

    hypotheses.sort(key=lambda h: h.score, reverse=True)
    total = sum(h.score for h in hypotheses[:2]) or 1.0

    primary = _to_count(hypotheses[0], hypotheses[0].score / total * 100.0)
    alternative = (
        _to_count(hypotheses[1], hypotheses[1].score / total * 100.0)
        if len(hypotheses) > 1 else None
    )

    return ElliottState(
        primary=primary,
        alternative=alternative,
        direction=primary.direction,
        confidence=primary.confidence,
        pivots_available=len(swings),
        reason=primary.reason,
    )


def _deduplicate(hypotheses: List[_Hypothesis]) -> List[_Hypothesis]:
    """Keep the best-scoring hypothesis per distinct reading.

    Two hypotheses describing the same wave in the same direction are one
    reading, not two — collapsing them is what keeps the ALTERNATIVE count a
    genuine alternative rather than an echo of the primary.
    """
    best: dict[tuple[str, str, str], _Hypothesis] = {}
    for hypothesis in hypotheses:
        key = (hypothesis.label, hypothesis.degree, hypothesis.direction)
        current = best.get(key)
        if current is None or hypothesis.score > current.score:
            best[key] = hypothesis
    return list(best.values())


def _to_count(hypothesis: _Hypothesis, raw_confidence: float) -> WaveCount:
    """Convert a scored hypothesis into a public WaveCount with capped confidence."""
    confidence = max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, raw_confidence))
    return WaveCount(
        label=hypothesis.label,
        degree=hypothesis.degree,
        direction=hypothesis.direction,
        confidence=round(confidence, 1),
        completion_pct=round(hypothesis.completion_pct, 1),
        projected_target=hypothesis.projected_target,
        wave_start=hypothesis.wave_start,
        pivots_used=hypothesis.pivots_used,
        rules_passed=hypothesis.rules_passed,
        rules_violated=hypothesis.rules_violated,
        guidelines_met=hypothesis.guidelines_met,
        reason=hypothesis.reason,
    )


# ─────────────────────────────────────────────
# Impulse counts (waves 1–5)
# ─────────────────────────────────────────────

def _impulse_hypotheses(window: List[Swing], price: float) -> List[_Hypothesis]:
    """Build impulse counts from a pivot window, if its orientation allows one.

    A bullish impulse starts from a swing LOW and alternates L-H-L-H-L-H; a
    bearish impulse is its mirror. The window's first pivot therefore fixes the
    orientation, and the number of pivots fixes which wave is in progress.
    """
    first = window[0]
    bullish = not first.is_high
    sign = 1.0 if bullish else -1.0
    direction = 'bullish' if bullish else 'bearish'
    p = [s.price for s in window]
    depth = len(window)

    # The wave in progress must be moving in the right direction from the last
    # pivot, otherwise this window does not describe the current move at all.
    def _advancing(from_price: float, up: bool) -> bool:
        return (price > from_price) if up else (price < from_price)

    rules_passed: List[str] = []
    rules_violated: List[str] = []
    guidelines: List[str] = []

    w1 = abs(p[1] - p[0]) if depth >= 2 else 0.0
    if w1 <= 0:
        return []

    # ── R1: wave 2 never retraces more than 100% of wave 1 ──
    if depth >= 3:
        if sign * (p[2] - p[0]) > 0:
            rules_passed.append('R1 wave 2 held above the start of wave 1')
        else:
            rules_violated.append('R1 wave 2 retraced beyond 100% of wave 1')
        retrace_2 = abs(p[1] - p[2]) / w1
        if 0.5 <= retrace_2 <= 0.618:
            guidelines.append(f'G1 wave 2 retraced {retrace_2:.1%} (textbook 50–61.8%)')
        elif 0.382 <= retrace_2 <= 0.786:
            guidelines.append(f'G1 wave 2 retraced {retrace_2:.1%} (acceptable)')

    w3 = abs(p[3] - p[2]) if depth >= 4 else 0.0
    if depth >= 4:
        # ── G2: wave 3 is commonly the extended wave ──
        ratio_3 = w3 / w1 if w1 > 0 else 0.0
        if ratio_3 >= 1.618:
            guidelines.append(f'G2 wave 3 extended to {ratio_3:.2f}x wave 1')
        elif ratio_3 > 1.0:
            guidelines.append(f'G2 wave 3 longer than wave 1 ({ratio_3:.2f}x)')

    # ── R3: wave 4 never enters wave 1's territory ──
    if depth >= 5:
        if sign * (p[4] - p[1]) > 0:
            rules_passed.append('R3 wave 4 stayed clear of wave 1')
        else:
            rules_violated.append('R3 wave 4 overlapped wave 1')
        retrace_4 = abs(p[3] - p[4]) / w3 if w3 > 0 else 0.0
        if 0.236 <= retrace_4 <= 0.5:
            guidelines.append(f'G3 wave 4 retraced {retrace_4:.1%} of wave 3')

    # ── R2: wave 3 is never the shortest of 1, 3, 5 ──
    if depth >= 5:
        w5 = abs(p[5] - p[4]) if depth >= 6 else abs(price - p[4])
        if w3 >= min(w1, w5) or w3 == 0:
            if w3 > 0:
                rules_passed.append('R2 wave 3 is not the shortest impulse wave')
        else:
            rules_violated.append('R2 wave 3 is the shortest impulse wave')

    # ── Which wave is in progress? ──
    if depth == 2:
        # After wave 1, price pulling back = wave 2.
        if _advancing(p[1], up=not bullish):
            label, wave_start = 'Wave 2', p[1]
            target = p[1] - sign * 0.618 * w1
            current_wave_dir = 'bearish' if bullish else 'bullish'
        else:
            label, wave_start = 'Wave 1', p[0]
            target = None
            current_wave_dir = direction
    elif depth == 3:
        if not _advancing(p[2], up=bullish):
            return []
        label, wave_start = 'Wave 3', p[2]
        target = p[2] + sign * 1.618 * w1
        current_wave_dir = direction
    elif depth == 4:
        if not _advancing(p[3], up=not bullish):
            return []
        label, wave_start = 'Wave 4', p[3]
        target = p[3] - sign * 0.382 * w3
        current_wave_dir = 'bearish' if bullish else 'bullish'
    elif depth == 5:
        if not _advancing(p[4], up=bullish):
            return []
        label, wave_start = 'Wave 5', p[4]
        # G4: wave 5 commonly equals wave 1, or 0.618x wave 3.
        target = p[4] + sign * max(w1, 0.618 * w3)
        guidelines.append('G4 wave 5 projected from wave 1 / 0.618x wave 3')
        current_wave_dir = direction
    else:  # depth == 6 — the impulse is complete, a correction is under way
        if not _advancing(p[5], up=not bullish):
            return []
        label, wave_start = 'Wave A', p[5]
        target = p[5] - sign * 0.382 * abs(p[5] - p[0])
        current_wave_dir = 'bearish' if bullish else 'bullish'

    score = _score(BASE_SCORE, depth, rules_violated, guidelines)
    return [_Hypothesis(
        label=label,
        degree=IMPULSE,
        direction=current_wave_dir,
        score=score,
        completion_pct=_completion(wave_start, target, price),
        projected_target=target,
        wave_start=wave_start,
        pivots_used=depth,
        rules_passed=rules_passed,
        rules_violated=rules_violated,
        guidelines_met=guidelines,
        reason=(
            f'{direction} impulse from {p[0]:.8f}; {label} in progress '
            f'({depth} pivots)'
        ),
    )]


# ─────────────────────────────────────────────
# Corrective counts (A-B-C)
# ─────────────────────────────────────────────

def _corrective_hypotheses(window: List[Swing], price: float) -> List[_Hypothesis]:
    """Build an A-B-C zigzag count from three pivots plus the live move.

    A = P0→P1, B = P1→P2, C = P2→price. For a valid zigzag the C leg must travel
    in A's direction; otherwise this window is not a correction in progress.
    """
    if len(window) < 3:
        return []
    p = [s.price for s in window[-3:]]

    a_size = abs(p[1] - p[0])
    b_size = abs(p[2] - p[1])
    if a_size <= 0:
        return []

    a_up = p[1] > p[0]
    c_up = price > p[2]
    if a_up != c_up:
        return []          # C is not travelling in A's direction

    sign = 1.0 if a_up else -1.0
    direction = 'bullish' if a_up else 'bearish'

    rules_passed: List[str] = []
    rules_violated: List[str] = []
    guidelines: List[str] = []

    # In a standard zigzag B does not fully retrace A.
    if b_size < a_size:
        rules_passed.append('B did not fully retrace A')
    else:
        rules_violated.append('B retraced beyond 100% of A (not a standard zigzag)')

    b_retrace = b_size / a_size
    if 0.382 <= b_retrace <= 0.786:
        guidelines.append(f'G5 wave B retraced {b_retrace:.1%} of wave A')

    # C commonly equals A; 1.618x A is the common extended case.
    target = p[2] + sign * a_size
    guidelines.append('G5 wave C projected at 1.0x wave A')

    score = _score(BASE_SCORE - 4.0, 3, rules_violated, guidelines)
    return [_Hypothesis(
        label='Wave C',
        degree=CORRECTIVE,
        direction=direction,
        score=score,
        completion_pct=_completion(p[2], target, price),
        projected_target=target,
        wave_start=p[2],
        pivots_used=3,
        rules_passed=rules_passed,
        rules_violated=rules_violated,
        guidelines_met=guidelines,
        reason=f'{direction} A-B-C zigzag; wave C in progress from {p[2]:.8f}',
    )]


# ─────────────────────────────────────────────
# Scoring helpers
# ─────────────────────────────────────────────

def _score(
    base: float, depth: int, violations: List[str], guidelines: List[str]
) -> float:
    """Combine evidence depth and guideline fit, penalising rule violations."""
    score = base + PIVOT_EVIDENCE_BONUS * depth + 6.0 * len(guidelines)
    for _ in violations:
        score *= RULE_VIOLATION_PENALTY
    return max(1.0, score)


def _completion(wave_start: float, target: Optional[float], price: float) -> float:
    """Progress of the wave in progress toward its projected target, 0–100."""
    if target is None:
        return 0.0
    span = target - wave_start
    if span == 0:
        return 0.0
    return max(0.0, min(100.0, (price - wave_start) / span * 100.0))
