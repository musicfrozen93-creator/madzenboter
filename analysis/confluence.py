"""Confluence Engine — stage 2 of the signal pipeline.

Aggregates the eight module votes into one direction, using the same weights as
the Quality Score so "what makes a good setup" is defined in exactly one place.

The higher timeframes are consulted here, not just observed: a setup whose
direction is contradicted by the trend rung is recorded as a hard conflict, and
hard conflicts force WAIT downstream.

"No setup" is a first-class outcome. The engine reports neutral rather than
picking the marginally larger of two near-equal sides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from analysis.modules import (
    BEARISH_VOTE,
    BULLISH_VOTE,
    MODULE_ORDER,
    ModuleVote,
    evaluate_modules,
)
from typing import FrozenSet
from analysis.structure import BEARISH, BULLISH, RANGE
from analysis.timeframes import MultiTimeframePicture, TREND
from v4.params import V4Params

logger = logging.getLogger(__name__)

# The winning side must hold at least this share of the cast directional weight.
# Below it the market is genuinely two-sided and the answer is WAIT.
MIN_AGREEMENT = 0.60

# A module opposing the winner with at least this strength is a real conflict,
# not noise.
CONFLICT_STRENGTH = 0.5


@dataclass
class ConfluenceResult:
    """The aggregated directional read."""

    direction: str = RANGE                       # 'bullish' | 'bearish' | 'range'
    votes: List[ModuleVote] = field(default_factory=list)
    bullish_weight: float = 0.0
    bearish_weight: float = 0.0
    agreement: float = 0.0                       # winner share of cast weight, 0–1
    conflicts: List[str] = field(default_factory=list)
    hard_conflicts: List[str] = field(default_factory=list)
    timeframe_agreement: float = 0.0             # 0–1 across the internal ladder
    reason: str = ''
    # The modules that participated in this read. A user-disabled optional module
    # is absent here (and from `votes`), so the scorers exclude it from BOTH the
    # earned points and the maximum — never treating it as evidence against.
    enabled_modules: FrozenSet[str] = field(default_factory=lambda: frozenset(MODULE_ORDER))

    @property
    def has_direction(self) -> bool:
        return self.direction in (BULLISH, BEARISH)

    @property
    def trade_side(self) -> Optional[str]:
        """The trade side ('long' / 'short') this direction implies."""
        if self.direction == BULLISH:
            return 'long'
        if self.direction == BEARISH:
            return 'short'
        return None

    def vote(self, module: str) -> Optional[ModuleVote]:
        """The vote cast by one module, if it was evaluated."""
        return next((v for v in self.votes if v.module == module), None)

    @property
    def blocked(self) -> bool:
        """True when the read cannot support a trade in any direction."""
        return (
            not self.has_direction
            or bool(self.hard_conflicts)
            or self.agreement < MIN_AGREEMENT
        )


class ConfluenceEngine:
    """Turns eight module votes plus timeframe agreement into one direction."""

    def __init__(self, params: Optional[V4Params] = None) -> None:
        self.params = params or V4Params()

    def evaluate(
        self,
        mtf: MultiTimeframePicture,
        enabled_modules: Optional[FrozenSet[str]] = None,
    ) -> ConfluenceResult:
        """Aggregate module votes for a multi-timeframe picture.

        Args:
            enabled_modules: The modules the user chose to include. ``None`` uses
                all modules (default). A module NOT in this set casts no vote: it
                is excluded from the aggregation, the conflict checks, and the
                score denominators — it is neutral/excluded, never negative.
        """
        active = frozenset(MODULE_ORDER) if enabled_modules is None else enabled_modules
        # Every module is still computed (pure and cheap), then filtered to the
        # active set so disabled modules simply do not participate.
        votes = [v for v in evaluate_modules(mtf) if v.module in active]

        bullish = sum(v.weighted_strength for v in votes if v.direction == BULLISH_VOTE)
        bearish = sum(v.weighted_strength for v in votes if v.direction == BEARISH_VOTE)
        cast = bullish + bearish

        if cast <= 0:
            return ConfluenceResult(
                direction=RANGE, votes=votes,
                timeframe_agreement=mtf.alignment,
                enabled_modules=active,
                reason='no module expressed a direction',
            )

        if bullish > bearish:
            direction, agreement = BULLISH, bullish / cast
        elif bearish > bullish:
            direction, agreement = BEARISH, bearish / cast
        else:
            return ConfluenceResult(
                direction=RANGE, votes=votes,
                bullish_weight=round(bullish, 2), bearish_weight=round(bearish, 2),
                timeframe_agreement=mtf.alignment,
                enabled_modules=active,
                reason='bullish and bearish evidence are exactly balanced',
            )

        side = 'long' if direction == BULLISH else 'short'
        conflicts = [
            f'{v.module}: {v.label} ({v.direction})'
            for v in votes
            if v.opposes(side) and v.strength >= CONFLICT_STRENGTH
        ]

        # ── Hard conflicts: the ones that must force WAIT ──
        hard: List[str] = []

        htf = mtf.higher_timeframe_direction
        opposite = BEARISH if direction == BULLISH else BULLISH
        if mtf.has_role(TREND) and htf == opposite:
            hard.append(
                f'higher timeframe ({mtf.view(TREND).timeframe}) is {htf}, '
                f'against a {direction} setup'
            )

        entry_structure = mtf.entry.structure
        if entry_structure.trend == opposite and entry_structure.strength >= 1.0:
            hard.append(f'market structure is firmly {opposite}')

        if agreement < MIN_AGREEMENT:
            hard.append(
                f'evidence is only {agreement:.0%} one-sided '
                f'(needs {MIN_AGREEMENT:.0%})'
            )

        result = ConfluenceResult(
            direction=direction,
            votes=votes,
            bullish_weight=round(bullish, 2),
            bearish_weight=round(bearish, 2),
            agreement=round(agreement, 4),
            conflicts=conflicts,
            hard_conflicts=hard,
            timeframe_agreement=mtf.alignment,
            enabled_modules=active,
            reason=(
                f'{direction} by {agreement:.0%} of cast module weight '
                f'({bullish:.1f} bullish vs {bearish:.1f} bearish)'
            ),
        )

        logger.debug(
            'CONFLUENCE | %s %s -> %s agreement=%.2f conflicts=%d hard=%d',
            mtf.symbol, mtf.selected_timeframe, direction,
            agreement, len(conflicts), len(hard),
        )
        return result

    @staticmethod
    def module_order() -> tuple[str, ...]:
        """The stable order modules are reported in."""
        return MODULE_ORDER
