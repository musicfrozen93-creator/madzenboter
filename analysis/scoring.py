"""Quality Score and Confidence Score — deterministic, rule-based, explainable.

Two different questions:

  QUALITY     How good is this SETUP?  Every point traces to one module's
              contribution, weighted exactly as `analysis.modules.MODULE_WEIGHTS`
              defines. Same inputs always produce the same score.

  CONFIDENCE  How certain is the ENGINE about that reading? Driven by agreement
              between the internal timeframes, wave certainty, data quality, and
              how much the evidence contradicts itself.

Both are pure arithmetic over the module votes. No AI, no randomness, no
hardcoded outputs — every point is produced by a stated rule and reported with
the rule that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from analysis.confluence import ConfluenceResult
from analysis.modules import (
    ELLIOTT,
    FVG,
    LIQUIDITY,
    MODULE_ORDER,
    MODULE_WEIGHTS,
    NEUTRAL_VOTE,
    ORDER_BLOCK,
    PATTERN,
    RSI,
    STRENGTH_MODULES,
    VOLUME,
    ModuleVote,
)
from analysis.structure import BEARISH, BULLISH
from analysis.timeframes import MultiTimeframePicture

# ── Quality grading bands ──
QUALITY_BANDS: tuple[tuple[int, str], ...] = (
    (95, 'Elite Setup'),
    (90, 'High Quality'),
    (80, 'Good'),
    (70, 'Average'),
    (0, 'Weak'),
)

# Absolute floor below which the evidence is too thin to act on at all.
#
# This is deliberately well below the 70 "Average" band. The grade COMMUNICATES
# quality; it is not the trade gate. What forces WAIT is contradicting evidence —
# an opposing higher timeframe, firmly opposing structure, or a market that is
# less than 60% one-sided (see `analysis.confluence`). A 60/100 signal with no
# conflicts is a real, honestly-graded "Weak" setup; suppressing it entirely
# would hide information the user asked for rather than protect them.
MIN_TRADEABLE_QUALITY = 50

# Credit a module gets when it has no directional opinion. It is not evidence
# for the trade, but it is not evidence against it either.
NEUTRAL_CREDIT = 0.5

# ── Confidence component budgets (total 100) ──
# Phase 2 added the Smart Money agreement components (order block, liquidity,
# FVG, pattern) and rebalanced the classical components down to fit.
CONFIDENCE_BUDGET = {
    'timeframe_agreement': 22,       # higher-timeframe agreement
    'trend_consistency': 12,         # trend agreement within each timeframe
    'wave_certainty': 10,
    'order_block_agreement': 8,
    'liquidity_agreement': 6,
    'fvg_agreement': 6,
    'pattern_agreement': 6,
    'volume_confirmation': 6,
    'rsi_confirmation': 6,
    'data_quality': 8,
    'indicator_agreement': 10,
}

# Which module backs each confidence component. When that module is disabled by
# the user, the component is dropped and the remaining component budgets are
# renormalised to 100 — so disabling an optional module does not penalise
# confidence any more than it penalises quality. Components mapped to None are
# always present (they come from required/aggregate data, not one optional
# module).
CONFIDENCE_COMPONENT_MODULE = {
    'timeframe_agreement': None,
    'trend_consistency': None,
    'wave_certainty': ELLIOTT,
    'order_block_agreement': ORDER_BLOCK,
    'liquidity_agreement': LIQUIDITY,
    'fvg_agreement': FVG,
    'pattern_agreement': PATTERN,
    'volume_confirmation': VOLUME,
    'rsi_confirmation': RSI,
    'data_quality': None,
    'indicator_agreement': None,
}

# Penalty per conflict, subtracted from confidence.
SOFT_CONFLICT_PENALTY = 4
HARD_CONFLICT_PENALTY = 12
MAX_CONFLICT_PENALTY = 30


@dataclass(frozen=True)
class ScoreComponent:
    """One line of a score breakdown."""

    name: str
    points: float
    max_points: float
    label: str
    detail: str

    @property
    def as_dict(self) -> dict:
        return {
            'name': self.name,
            'points': round(self.points, 1),
            'max_points': self.max_points,
            'label': self.label,
            'detail': self.detail,
        }


@dataclass
class Score:
    """A 0–100 score with the components that produced it."""

    value: int
    grade: str
    components: List[ScoreComponent] = field(default_factory=list)

    @property
    def factors(self) -> List[str]:
        """One human-readable line per scoring component."""
        return [
            f'{c.label}: {c.points:.0f}/{c.max_points:.0f} — {c.detail}'
            for c in self.components
        ]


def grade_for(value: int) -> str:
    """The interpretation band a quality score falls into."""
    for threshold, label in QUALITY_BANDS:
        if value >= threshold:
            return label
    return 'Weak'


def _effective_side(confluence: ConfluenceResult) -> str:
    """The side quality is measured against.

    When confluence found a direction, that is the side. When it did not, the
    numerically stronger side is used so the score still explains WHY the setup
    is weak rather than reporting nothing.
    """
    side = confluence.trade_side
    if side:
        return side
    return 'long' if confluence.bullish_weight >= confluence.bearish_weight else 'short'


class QualityScorer:
    """Scores how structurally sound a setup is, 0–100."""

    stage = 'quality'
    implemented = True

    def score(
        self, mtf: MultiTimeframePicture, confluence: ConfluenceResult
    ) -> Score:
        """Award each ENABLED module its weight scaled by how well it supports the side.

            agrees   → weight x strength          (full credit)
            neutral  → weight x strength x 0.5    (partial — not evidence against)
            opposes  → 0                          (and recorded as a conflict)

        ATR is always directionless: its strength IS volatility suitability, so
        it takes full credit for being suitable.

        DISABLED modules (not in ``confluence.enabled_modules``) are excluded
        entirely: they earn no points AND their weight is removed from the
        maximum, so the final 0–100 value is ``earned / enabled_weight * 100``.
        A disabled module therefore never lowers the score — it is neutral, not
        negative. With every module enabled the enabled weight is 100 and this
        reduces to the original ``sum(points)``, so default behaviour is
        unchanged.
        """
        side = _effective_side(confluence)
        enabled = confluence.enabled_modules
        components: List[ScoreComponent] = []
        enabled_weight = 0

        for module in MODULE_ORDER:
            if module not in enabled:
                # User-disabled: not part of the setup at all. Excluded from both
                # the earned points and the maximum — never a penalty.
                continue

            vote = confluence.vote(module)
            max_points = MODULE_WEIGHTS[module]
            enabled_weight += max_points
            if vote is None:
                components.append(ScoreComponent(
                    module, 0.0, max_points, 'Not evaluated',
                    'module did not report',
                ))
                continue

            if module in STRENGTH_MODULES:
                # ATR (volatility) and ADX (trend strength) confirm tradeability,
                # not direction — they earn their weight by suitability alone.
                points = max_points * vote.strength
                basis = 'context/strength confirmation'
            elif vote.agrees_with(side):
                points = max_points * vote.strength
                basis = f'supports the {side} setup'
            elif vote.direction == NEUTRAL_VOTE:
                points = max_points * vote.strength * NEUTRAL_CREDIT
                basis = 'no directional opinion'
            else:
                points = 0.0
                basis = f'opposes the {side} setup'

            components.append(ScoreComponent(
                name=module,
                points=points,
                max_points=max_points,
                label=vote.label,
                detail=f'{vote.detail} — {basis}',
            ))

        earned = sum(c.points for c in components)
        # Normalise against the ENABLED weight so disabling optional modules
        # neither penalises nor makes the tradeable threshold impossible.
        normalized = (earned / enabled_weight * 100) if enabled_weight > 0 else 0.0
        total = max(0, min(100, int(round(normalized))))
        return Score(value=total, grade=grade_for(total), components=components)


class ConfidenceScorer:
    """Scores how certain the engine is about its own reading, 0–100."""

    stage = 'confidence'
    implemented = True

    def score(
        self, mtf: MultiTimeframePicture, confluence: ConfluenceResult
    ) -> Score:
        """Combine agreement, certainty, and data quality, less conflict penalties.

        Components backed by a user-disabled module are removed and the remaining
        component budgets are renormalised to 100, so disabling an optional
        module never penalises confidence (mirrors the Quality Score treatment).
        With every module enabled the kept budgets already total 100, so the
        default behaviour is unchanged.
        """
        side = _effective_side(confluence)
        enabled = confluence.enabled_modules
        built: List[ScoreComponent] = []

        built.append(self._timeframe_agreement(mtf, side))
        built.append(self._trend_consistency(mtf))
        built.append(self._wave_certainty(confluence))
        # Smart Money agreement (Phase 2).
        built.append(self._directional_confirmation(
            confluence, ORDER_BLOCK, 'order_block_agreement', side, 'Order block agreement'
        ))
        built.append(self._directional_confirmation(
            confluence, LIQUIDITY, 'liquidity_agreement', side, 'Liquidity agreement'
        ))
        built.append(self._directional_confirmation(
            confluence, FVG, 'fvg_agreement', side, 'FVG agreement'
        ))
        built.append(self._directional_confirmation(
            confluence, PATTERN, 'pattern_agreement', side, 'Pattern agreement'
        ))
        built.append(self._directional_confirmation(
            confluence, VOLUME, 'volume_confirmation', side, 'Volume confirmation'
        ))
        built.append(self._directional_confirmation(
            confluence, RSI, 'rsi_confirmation', side, 'RSI confirmation'
        ))
        built.append(self._data_quality(mtf))
        built.append(self._indicator_agreement(confluence, side))

        # Keep only components whose backing module is enabled (None = always).
        kept = [
            c for c in built
            if CONFIDENCE_COMPONENT_MODULE.get(c.name) in (None,)
            or CONFIDENCE_COMPONENT_MODULE.get(c.name) in enabled
        ]
        kept_budget = sum(c.max_points for c in kept)
        # Renormalise the kept components back to a 100-point base so a dropped
        # component leaves no hole in the score.
        factor = (100.0 / kept_budget) if kept_budget > 0 else 0.0
        components: List[ScoreComponent] = [
            ScoreComponent(c.name, c.points * factor, c.max_points * factor, c.label, c.detail)
            for c in kept
        ]

        subtotal = sum(c.points for c in components)
        penalty_component = self._conflict_penalty(confluence)
        components.append(penalty_component)

        total = int(round(subtotal + penalty_component.points))
        total = max(0, min(100, total))
        return Score(value=total, grade=_confidence_grade(total), components=components)

    # ── Components ──

    def _timeframe_agreement(
        self, mtf: MultiTimeframePicture, side: str
    ) -> ScoreComponent:
        """How much of the internal timeframe ladder agrees with the direction."""
        budget = CONFIDENCE_BUDGET['timeframe_agreement']
        agreement = mtf.agreement_with(side)
        analysed = ', '.join(f'{v.role}' for v in mtf.views)
        return ScoreComponent(
            'timeframe_agreement', budget * agreement, budget,
            'Timeframe agreement',
            f'{agreement:.0%} of the internal ladder ({analysed}) agrees',
        )

    def _trend_consistency(self, mtf: MultiTimeframePicture) -> ScoreComponent:
        """How unanimous each timeframe is internally (regime, EMA, structure)."""
        budget = CONFIDENCE_BUDGET['trend_consistency']
        convictions = [v.conviction for v in mtf.views]
        average = sum(convictions) / len(convictions) if convictions else 0.0
        return ScoreComponent(
            'trend_consistency', budget * average, budget,
            'Trend consistency',
            f'{average:.0%} average agreement between regime, EMA and structure',
        )

    def _wave_certainty(self, confluence: ConfluenceResult) -> ScoreComponent:
        """How decisively the primary Elliott count beat its alternative."""
        budget = CONFIDENCE_BUDGET['wave_certainty']
        vote = confluence.vote(ELLIOTT)
        if vote is None or vote.strength <= 0:
            return ScoreComponent(
                'wave_certainty', 0.0, budget, 'Wave certainty',
                'no wave count could be established',
            )
        return ScoreComponent(
            'wave_certainty', budget * vote.strength, budget,
            'Wave certainty', vote.detail,
        )

    def _directional_confirmation(
        self,
        confluence: ConfluenceResult,
        module: str,
        name: str,
        side: str,
        label: str,
    ) -> ScoreComponent:
        """Full credit when a module confirms the side, none when it opposes."""
        budget = CONFIDENCE_BUDGET[name]
        vote = confluence.vote(module)
        if vote is None:
            return ScoreComponent(name, 0.0, budget, label, 'not evaluated')
        if vote.agrees_with(side):
            return ScoreComponent(
                name, budget * vote.strength, budget, label, vote.detail
            )
        if vote.direction == NEUTRAL_VOTE:
            return ScoreComponent(
                name, budget * 0.3, budget, label, f'{vote.detail} — neutral'
            )
        return ScoreComponent(
            name, 0.0, budget, label, f'{vote.detail} — contradicts the setup'
        )

    def _data_quality(self, mtf: MultiTimeframePicture) -> ScoreComponent:
        """Completeness of the data the reading was built on."""
        budget = CONFIDENCE_BUDGET['data_quality']
        entry = mtf.entry
        notes: List[str] = []
        fraction = 1.0

        # Every ladder rung that could not be analysed removes certainty.
        planned = len(mtf.ladder.rungs)
        analysed = len(mtf.views)
        if analysed < planned:
            fraction *= analysed / planned
            notes.append(f'{planned - analysed} of {planned} timeframes unavailable')

        # A missing live quote means the spread could not be verified.
        if entry.quote is None:
            fraction *= 0.85
            notes.append('live quote unavailable')

        # Unclean market conditions make every reading less trustworthy.
        if not entry.market_conditions_clean:
            fraction *= 0.6
            notes.append(f'market conditions unclean ({entry.blocking_condition})')

        # Too few pivots means structure, Fibonacci and Elliott are all thin.
        pivots = len(entry.structure.swings)
        if pivots < 6:
            fraction *= 0.7
            notes.append(f'only {pivots} confirmed swings')

        detail = '; '.join(notes) if notes else (
            f'{len(entry.candles)} candles across {analysed} timeframes, complete'
        )
        return ScoreComponent(
            'data_quality', budget * fraction, budget, 'Data quality', detail,
        )

    def _indicator_agreement(
        self, confluence: ConfluenceResult, side: str
    ) -> ScoreComponent:
        """Share of directional modules that agree with the chosen side."""
        budget = CONFIDENCE_BUDGET['indicator_agreement']
        directional = [v for v in confluence.votes if v.direction != NEUTRAL_VOTE]
        if not directional:
            return ScoreComponent(
                'indicator_agreement', 0.0, budget, 'Indicator agreement',
                'no module expressed a direction',
            )
        agreeing = [v for v in directional if v.agrees_with(side)]
        fraction = len(agreeing) / len(directional)
        return ScoreComponent(
            'indicator_agreement', budget * fraction, budget,
            'Indicator agreement',
            f'{len(agreeing)} of {len(directional)} directional modules agree',
        )

    def _conflict_penalty(self, confluence: ConfluenceResult) -> ScoreComponent:
        """Subtract for contradicting evidence — the engine says so out loud."""
        penalty = (
            SOFT_CONFLICT_PENALTY * len(confluence.conflicts)
            + HARD_CONFLICT_PENALTY * len(confluence.hard_conflicts)
        )
        penalty = min(MAX_CONFLICT_PENALTY, penalty)
        if penalty == 0:
            detail = 'no contradicting evidence'
        else:
            items = confluence.hard_conflicts + confluence.conflicts
            detail = '; '.join(items[:3])
        return ScoreComponent(
            'conflict_penalty', -float(penalty), 0.0, 'Conflicts', detail,
        )


def _confidence_grade(value: int) -> str:
    """Plain-language band for a confidence score."""
    if value >= 85:
        return 'Very High'
    if value >= 70:
        return 'High'
    if value >= 55:
        return 'Moderate'
    if value >= 40:
        return 'Low'
    return 'Very Low'
