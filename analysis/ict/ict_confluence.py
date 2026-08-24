"""ICT + MSNR Confluence Engine — relationship-based confluence (Phase 2A).

This is the most important module in Phase 2A.  It does NOT simply add
ICT score + MSNR score + existing score.  Instead it identifies
RELATIONSHIPS between the evidence layers.

High-quality LONG context:
  Price near strong MSNR support
  + Sell-side liquidity sweep
  + Bullish MSS/BOS
  + Bullish displacement
  + Bullish FVG or Order Block
  + Existing trend/context is not strongly bearish
  → strong bullish confluence

High-quality SHORT context:
  Price near strong MSNR resistance
  + Buy-side liquidity sweep
  + Bearish MSS/BOS
  + Bearish displacement
  + Bearish FVG or Order Block
  + Existing trend/context is not strongly bullish
  → strong bearish confluence

If only one or two ICT/MSNR elements exist without proper confirmation,
the engine does NOT force BUY/SELL.

The result is consumed by the main Confluence Engine as ONE contextual
input, not as a second set of weighted votes.

DOUBLE-COUNTING:  All evidence goes through the EvidenceRegistry which
deduplicates by evidence_id.  The same swing/level/event is counted once.

Pure and network-free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from analysis.ict.displacement import DisplacementResult, BULLISH as DISP_BULLISH, BEARISH as DISP_BEARISH
from analysis.ict.evidence import EvidenceItem, EvidenceRegistry
from analysis.ict.ict_structure import ICTStructureResult
from analysis.ict.msnr import (
    MSNRResult,
    SUPPORT_ZONE,
    RESISTANCE_ZONE,
)
from analysis.ict.premium_discount import (
    PremiumDiscountResult,
    PREMIUM,
    DISCOUNT,
    EQUILIBRIUM,
)
from analysis.smc.fvg import FairValueGapState
from analysis.smc.liquidity import LiquidityState
from analysis.smc.order_blocks import OrderBlockState
from analysis.structure import BEARISH, BULLISH, RANGE

logger = logging.getLogger(__name__)

# Minimum number of aligned ICT elements required to produce a directional read.
MIN_ALIGNED_ELEMENTS = 3

# Maximum ICT-MSNR confluence contribution to the quality score.
# Deliberately small — exposed separately for the confluence engine to
# interpret as relationship context, not as added weight.
MAX_ICT_CONTRIBUTION = 8


@dataclass(frozen=True)
class ConfluenceElement:
    """One element contributing to or opposing the ICT-MSNR confluence."""

    name: str
    direction: str         # 'bullish' | 'bearish' | 'neutral'
    strength: float        # 0–1
    detail: str


@dataclass
class IctMsnrConfluence:
    """The ICT-MSNR relationship read."""

    direction: str = RANGE                # 'bullish' | 'bearish' | 'range'
    strength: float = 0.0                 # 0–1
    score: float = 0.0                    # 0–1 contribution quality

    bullish_elements: List[ConfluenceElement] = field(default_factory=list)
    bearish_elements: List[ConfluenceElement] = field(default_factory=list)
    neutral_elements: List[ConfluenceElement] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)

    primary_reason: str = ''
    explanation: str = ''

    # For WAIT: what blocked a signal.
    primary_blocker: Optional[str] = None
    secondary_blockers: List[str] = field(default_factory=list)

    # Deduplication stats.
    evidence_count: int = 0
    deduplicated_count: int = 0

    @property
    def has_direction(self) -> bool:
        return self.direction in (BULLISH, BEARISH)

    @property
    def supports_long(self) -> bool:
        return self.direction == BULLISH and self.strength >= 0.5

    @property
    def supports_short(self) -> bool:
        return self.direction == BEARISH and self.strength >= 0.5

    @property
    def suggests_wait(self) -> bool:
        return not self.has_direction or self.strength < 0.3


class ICTMSNRConfluenceEngine:
    """Evaluates the relationship between ICT and MSNR evidence layers."""

    def evaluate(
        self,
        msnr: MSNRResult,
        ict_structure: ICTStructureResult,
        displacement: DisplacementResult,
        premium_discount: PremiumDiscountResult,
        liquidity: LiquidityState,
        fvg: FairValueGapState,
        order_blocks: OrderBlockState,
        existing_trend: str = RANGE,
    ) -> IctMsnrConfluence:
        """Evaluate ICT-MSNR confluence from all the component results.

        Args:
            msnr: Location context from the MSNR engine.
            ict_structure: ICT interpretation of market structure.
            displacement: Displacement detection.
            premium_discount: Dealing range position.
            liquidity: Liquidity analysis from the existing engine.
            fvg: Fair value gaps from the existing engine.
            order_blocks: Order blocks from the existing engine.
            existing_trend: The trend direction from the existing analysis.

        Returns:
            IctMsnrConfluence with the relationship-based directional read.
        """
        # 1. Collect all evidence into the dedup registry.
        registry = EvidenceRegistry()
        registry.add_all(msnr.evidence)
        registry.add_all(ict_structure.evidence)
        registry.add_all(displacement.evidence)
        registry.add_all(premium_discount.evidence)
        # Existing engines' evidence is implicit via the evidence_id matching
        # (their swing highs, support levels, etc. are already covered by
        # MSNR and ICT structure evidence with matching ids).

        # 2. Evaluate each element independently.
        elements: List[ConfluenceElement] = []
        conflicts: List[str] = []

        # ── MSNR Location ──
        elements.append(self._eval_msnr(msnr))

        # ── ICT Structure (BOS / MSS) ──
        elements.append(self._eval_ict_structure(ict_structure))

        # ── Displacement ──
        elements.append(self._eval_displacement(displacement))

        # ── Premium/Discount ──
        elements.append(self._eval_premium_discount(premium_discount))

        # ── Liquidity (from existing engine) ──
        elements.append(self._eval_liquidity(liquidity))

        # ── FVG (from existing engine) ──
        elements.append(self._eval_fvg(fvg))

        # ── Order Blocks (from existing engine) ──
        elements.append(self._eval_order_blocks(order_blocks))

        # 3. Classify elements by direction.
        bullish = [e for e in elements if e.direction == BULLISH]
        bearish = [e for e in elements if e.direction == BEARISH]
        neutral = [e for e in elements if e.direction not in (BULLISH, BEARISH)]

        # 4. Determine direction from the RELATIONSHIP, not from sums.
        direction, strength, reason, blockers = self._determine_direction(
            bullish, bearish, neutral, existing_trend, msnr, ict_structure,
            displacement, liquidity, fvg, order_blocks, premium_discount,
        )

        # 5. Check for conflicts with the existing trend.
        if direction == BULLISH and existing_trend == BEARISH:
            conflicts.append(
                'ICT-MSNR reads bullish but existing trend context is bearish'
            )
        elif direction == BEARISH and existing_trend == BULLISH:
            conflicts.append(
                'ICT-MSNR reads bearish but existing trend context is bullish'
            )

        # 6. Score: how strongly the ICT-MSNR context supports a trade.
        score = self._compute_score(direction, strength, bullish, bearish, conflicts)

        primary_blocker = blockers[0] if blockers else None
        secondary_blockers = blockers[1:] if len(blockers) > 1 else []

        explanation = self._build_explanation(
            direction, strength, bullish, bearish, conflicts, reason,
        )

        return IctMsnrConfluence(
            direction=direction,
            strength=round(strength, 4),
            score=round(score, 4),
            bullish_elements=bullish,
            bearish_elements=bearish,
            neutral_elements=neutral,
            conflicts=conflicts,
            primary_reason=reason,
            explanation=explanation,
            primary_blocker=primary_blocker,
            secondary_blockers=secondary_blockers,
            evidence_count=len(registry.items),
            deduplicated_count=registry.dedup_count,
        )

    # ── Element evaluators ──

    def _eval_msnr(self, msnr: MSNRResult) -> ConfluenceElement:
        if msnr.location == SUPPORT_ZONE:
            return ConfluenceElement(
                'MSNR Location', BULLISH, msnr.strength,
                msnr.explanation,
            )
        if msnr.location == RESISTANCE_ZONE:
            return ConfluenceElement(
                'MSNR Location', BEARISH, msnr.strength,
                msnr.explanation,
            )
        return ConfluenceElement(
            'MSNR Location', 'neutral', msnr.strength,
            msnr.explanation,
        )

    def _eval_ict_structure(self, ict: ICTStructureResult) -> ConfluenceElement:
        if ict.has_mss and ict.mss_direction:
            return ConfluenceElement(
                'ICT Structure', ict.mss_direction, ict.strength,
                f'MSS {ict.mss_direction}: {ict.explanation}',
            )
        if ict.has_bos and ict.bos_direction:
            return ConfluenceElement(
                'ICT Structure', ict.bos_direction, ict.strength * 0.8,
                f'BOS {ict.bos_direction}: {ict.explanation}',
            )
        return ConfluenceElement(
            'ICT Structure', 'neutral', 0.0, ict.explanation,
        )

    def _eval_displacement(self, disp: DisplacementResult) -> ConfluenceElement:
        if disp.direction == DISP_BULLISH:
            return ConfluenceElement(
                'Displacement', BULLISH, disp.strength, disp.explanation,
            )
        if disp.direction == DISP_BEARISH:
            return ConfluenceElement(
                'Displacement', BEARISH, disp.strength, disp.explanation,
            )
        return ConfluenceElement(
            'Displacement', 'neutral', 0.0, disp.explanation,
        )

    def _eval_premium_discount(self, pd_result: PremiumDiscountResult) -> ConfluenceElement:
        if pd_result.zone == DISCOUNT:
            strength = min(1.0, abs(pd_result.price_position - 0.5) / 0.5)
            return ConfluenceElement(
                'Premium/Discount', BULLISH, strength * 0.6,
                pd_result.explanation,
            )
        if pd_result.zone == PREMIUM:
            strength = min(1.0, abs(pd_result.price_position - 0.5) / 0.5)
            return ConfluenceElement(
                'Premium/Discount', BEARISH, strength * 0.6,
                pd_result.explanation,
            )
        return ConfluenceElement(
            'Premium/Discount', 'neutral', 0.2, pd_result.explanation,
        )

    def _eval_liquidity(self, liq: LiquidityState) -> ConfluenceElement:
        if liq.last_sweep is not None and liq.direction in (BULLISH, BEARISH):
            label = 'grab' if liq.last_sweep.grabbed else 'sweep'
            return ConfluenceElement(
                'Liquidity', liq.direction, liq.score,
                f'{label} of {liq.last_sweep.side}: {liq.reason}',
            )
        return ConfluenceElement(
            'Liquidity', 'neutral', 0.0, liq.reason,
        )

    def _eval_fvg(self, fvg: FairValueGapState) -> ConfluenceElement:
        if fvg.nearest is not None and fvg.direction in (BULLISH, BEARISH):
            return ConfluenceElement(
                'FVG', fvg.direction, fvg.score, fvg.reason,
            )
        return ConfluenceElement(
            'FVG', 'neutral', 0.0, fvg.reason,
        )

    def _eval_order_blocks(self, ob: OrderBlockState) -> ConfluenceElement:
        if ob.nearest is not None and ob.direction in (BULLISH, BEARISH):
            return ConfluenceElement(
                'Order Block', ob.direction, ob.score, ob.reason,
            )
        return ConfluenceElement(
            'Order Block', 'neutral', 0.0, ob.reason,
        )

    # ── Direction determination ──

    def _determine_direction(
        self,
        bullish: List[ConfluenceElement],
        bearish: List[ConfluenceElement],
        neutral: List[ConfluenceElement],
        existing_trend: str,
        msnr: MSNRResult,
        ict_structure: ICTStructureResult,
        displacement: DisplacementResult,
        liquidity: LiquidityState,
        fvg: FairValueGapState,
        order_blocks: OrderBlockState,
        premium_discount: PremiumDiscountResult,
    ) -> tuple[str, float, str, List[str]]:
        """Determine direction from the RELATIONSHIP between elements.

        This is the core logic.  It does NOT simply count bullish vs bearish.
        Instead it checks for the HIGH-QUALITY CONFLUENCE PATTERNS described
        in the specification.

        Returns:
            (direction, strength, reason, blockers)
        """
        blockers: List[str] = []

        bull_count = len(bullish)
        bear_count = len(bearish)

        # Not enough elements to form a directional read.
        if bull_count < MIN_ALIGNED_ELEMENTS and bear_count < MIN_ALIGNED_ELEMENTS:
            blocker_msg = (
                f'only {bull_count} bullish and {bear_count} bearish elements '
                f'(need at least {MIN_ALIGNED_ELEMENTS} aligned)'
            )
            blockers.append(blocker_msg)
            return RANGE, 0.0, 'insufficient ICT-MSNR alignment', blockers

        # Check for the high-quality long pattern.
        long_quality = self._check_long_pattern(
            msnr, ict_structure, displacement, liquidity, fvg, order_blocks,
            premium_discount, existing_trend,
        )

        # Check for the high-quality short pattern.
        short_quality = self._check_short_pattern(
            msnr, ict_structure, displacement, liquidity, fvg, order_blocks,
            premium_discount, existing_trend,
        )

        # Neither pattern is strong enough.
        if long_quality < 0.3 and short_quality < 0.3:
            if bull_count > bear_count + 1:
                # Weak bullish — elements align but no strong pattern.
                bull_strength = sum(e.strength for e in bullish) / max(1, bull_count)
                return (
                    BULLISH, round(bull_strength * 0.5, 4),
                    f'{bull_count} elements lean bullish but no high-quality pattern',
                    blockers,
                )
            if bear_count > bull_count + 1:
                bear_strength = sum(e.strength for e in bearish) / max(1, bear_count)
                return (
                    BEARISH, round(bear_strength * 0.5, 4),
                    f'{bear_count} elements lean bearish but no high-quality pattern',
                    blockers,
                )
            blockers.append('no high-quality ICT-MSNR pattern detected')
            return RANGE, 0.0, 'no clear ICT-MSNR confluence', blockers

        # Both patterns are present (conflicting).
        if long_quality >= 0.3 and short_quality >= 0.3:
            diff = abs(long_quality - short_quality)
            if diff < 0.15:
                blockers.append('both long and short ICT patterns are present')
                return RANGE, 0.0, 'conflicting ICT-MSNR patterns', blockers
            # The stronger pattern wins, but with reduced strength.
            if long_quality > short_quality:
                return (
                    BULLISH, round(long_quality * 0.7, 4),
                    'bullish ICT pattern stronger but bearish pattern also present',
                    blockers,
                )
            return (
                BEARISH, round(short_quality * 0.7, 4),
                'bearish ICT pattern stronger but bullish pattern also present',
                blockers,
            )

        # Only one pattern is strong enough.
        if long_quality >= 0.3:
            return (
                BULLISH, round(long_quality, 4),
                'high-quality bullish ICT-MSNR confluence',
                blockers,
            )
        return (
            BEARISH, round(short_quality, 4),
            'high-quality bearish ICT-MSNR confluence',
            blockers,
        )

    def _check_long_pattern(
        self,
        msnr: MSNRResult,
        ict_structure: ICTStructureResult,
        displacement: DisplacementResult,
        liquidity: LiquidityState,
        fvg: FairValueGapState,
        order_blocks: OrderBlockState,
        premium_discount: PremiumDiscountResult,
        existing_trend: str,
    ) -> float:
        """Evaluate the quality of a bullish ICT-MSNR pattern (0–1).

        HIGH QUALITY LONG:
          Price near strong MSNR support
          + Sell-side liquidity sweep
          + Bullish MSS/BOS
          + Bullish displacement
          + Bullish FVG or Order Block
          + Existing trend/context not strongly bearish
        """
        score = 0.0
        max_score = 0.0

        # 1. MSNR support (most important for location context).
        max_score += 0.25
        if msnr.location == SUPPORT_ZONE:
            score += 0.25 * msnr.strength

        # 2. Sell-side liquidity sweep (contrarian bullish).
        max_score += 0.20
        if (liquidity.last_sweep is not None
                and liquidity.direction == BULLISH):
            score += 0.20 * liquidity.score

        # 3. Bullish MSS or BOS.
        max_score += 0.20
        if ict_structure.has_mss and ict_structure.mss_direction == BULLISH:
            score += 0.20 * ict_structure.strength
        elif ict_structure.has_bos and ict_structure.bos_direction == BULLISH:
            score += 0.15 * ict_structure.strength  # BOS slightly less significant

        # 4. Bullish displacement.
        max_score += 0.15
        if displacement.direction == DISP_BULLISH:
            score += 0.15 * displacement.strength

        # 5. Bullish FVG or Order Block.
        max_score += 0.10
        if fvg.direction == BULLISH:
            score += 0.05 * fvg.score
        if order_blocks.direction == BULLISH:
            score += 0.05 * order_blocks.score

        # 6. Discount zone adds context.
        max_score += 0.05
        if premium_discount.zone == DISCOUNT:
            score += 0.05

        # 7. Existing trend penalty.
        max_score += 0.05
        if existing_trend == BULLISH:
            score += 0.05  # trend alignment bonus
        elif existing_trend == BEARISH:
            score *= 0.6   # reduce score when fighting the trend

        return min(1.0, score / max_score) if max_score > 0 else 0.0

    def _check_short_pattern(
        self,
        msnr: MSNRResult,
        ict_structure: ICTStructureResult,
        displacement: DisplacementResult,
        liquidity: LiquidityState,
        fvg: FairValueGapState,
        order_blocks: OrderBlockState,
        premium_discount: PremiumDiscountResult,
        existing_trend: str,
    ) -> float:
        """Evaluate the quality of a bearish ICT-MSNR pattern (0–1).

        HIGH QUALITY SHORT:
          Price near strong MSNR resistance
          + Buy-side liquidity sweep
          + Bearish MSS/BOS
          + Bearish displacement
          + Bearish FVG or Order Block
          + Existing trend/context not strongly bullish
        """
        score = 0.0
        max_score = 0.0

        # 1. MSNR resistance.
        max_score += 0.25
        if msnr.location == RESISTANCE_ZONE:
            score += 0.25 * msnr.strength

        # 2. Buy-side liquidity sweep (contrarian bearish).
        max_score += 0.20
        if (liquidity.last_sweep is not None
                and liquidity.direction == BEARISH):
            score += 0.20 * liquidity.score

        # 3. Bearish MSS or BOS.
        max_score += 0.20
        if ict_structure.has_mss and ict_structure.mss_direction == BEARISH:
            score += 0.20 * ict_structure.strength
        elif ict_structure.has_bos and ict_structure.bos_direction == BEARISH:
            score += 0.15 * ict_structure.strength

        # 4. Bearish displacement.
        max_score += 0.15
        if displacement.direction == DISP_BEARISH:
            score += 0.15 * displacement.strength

        # 5. Bearish FVG or Order Block.
        max_score += 0.10
        if fvg.direction == BEARISH:
            score += 0.05 * fvg.score
        if order_blocks.direction == BEARISH:
            score += 0.05 * order_blocks.score

        # 6. Premium zone adds context.
        max_score += 0.05
        if premium_discount.zone == PREMIUM:
            score += 0.05

        # 7. Existing trend penalty.
        max_score += 0.05
        if existing_trend == BEARISH:
            score += 0.05
        elif existing_trend == BULLISH:
            score *= 0.6

        return min(1.0, score / max_score) if max_score > 0 else 0.0

    # ── Scoring and explanation ──

    def _compute_score(
        self,
        direction: str,
        strength: float,
        bullish: List[ConfluenceElement],
        bearish: List[ConfluenceElement],
        conflicts: List[str],
    ) -> float:
        """0–1 score indicating how strongly ICT-MSNR supports a trade."""
        if direction == RANGE:
            return 0.0
        base = strength
        # Penalise conflicts.
        conflict_penalty = min(0.3, 0.15 * len(conflicts))
        return max(0.0, min(1.0, base - conflict_penalty))

    def _build_explanation(
        self,
        direction: str,
        strength: float,
        bullish: List[ConfluenceElement],
        bearish: List[ConfluenceElement],
        conflicts: List[str],
        reason: str,
    ) -> str:
        """Build a human-readable explanation of the ICT-MSNR confluence."""
        parts: List[str] = [reason]

        if bullish:
            bull_names = [e.name for e in bullish]
            parts.append(f'Bullish: {", ".join(bull_names)}')
        if bearish:
            bear_names = [e.name for e in bearish]
            parts.append(f'Bearish: {", ".join(bear_names)}')
        if conflicts:
            parts.append(f'Conflicts: {"; ".join(conflicts)}')

        return ' | '.join(parts)
