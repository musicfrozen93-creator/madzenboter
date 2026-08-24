"""Premium / Discount Engine — dealing-range position (Phase 2A).

Given the current dealing range (the recent swing high and swing low), price
can sit in:

  PREMIUM       above the 50% mark — expensive to buy, good to sell
  EQUILIBRIUM   near the 50% mark — no directional edge from location
  DISCOUNT      below the 50% mark — cheap to buy, expensive to sell

This is purely contextual evidence — it never creates a signal on its own.

Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from analysis.ict.evidence import EvidenceItem, make_evidence_id
from analysis.structure import Swing

PREMIUM = 'PREMIUM'
EQUILIBRIUM = 'EQUILIBRIUM'
DISCOUNT = 'DISCOUNT'
UNAVAILABLE = 'UNAVAILABLE'

# The band around the midpoint that counts as equilibrium.
EQUILIBRIUM_BAND = 0.10  # ±10% of the dealing range from the midpoint


@dataclass
class PremiumDiscountResult:
    """Where price sits within the current dealing range."""

    zone: str = UNAVAILABLE
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    midpoint: Optional[float] = None
    price_position: float = 0.5
    explanation: str = 'insufficient swing data'
    evidence: List[EvidenceItem] = field(default_factory=list)


class PremiumDiscountEngine:
    """Determines premium/equilibrium/discount from the dealing range."""

    def analyze(
        self,
        swings: List[Swing],
        price: float,
    ) -> PremiumDiscountResult:
        """Classify where price sits within the current dealing range.

        The dealing range is defined by the highest swing high and lowest
        swing low from the recent swings.  At least 4 swings are required
        (same minimum as the structure engine).

        Args:
            swings: Confirmed swings from the structure engine.
            price: Current price.

        Returns:
            PremiumDiscountResult with the zone classification.
        """
        if not swings or len(swings) < 4 or price <= 0:
            return PremiumDiscountResult()

        highs = [s.price for s in swings if s.is_high]
        lows = [s.price for s in swings if not s.is_high]

        if not highs or not lows:
            return PremiumDiscountResult()

        range_high = max(highs)
        range_low = min(lows)
        dealing_range = range_high - range_low

        if dealing_range <= 0:
            return PremiumDiscountResult(
                range_high=range_high, range_low=range_low,
                explanation='dealing range is zero (flat market)',
            )

        midpoint = range_low + dealing_range / 2.0
        position = (price - range_low) / dealing_range
        position = max(0.0, min(1.0, position))

        # Classify based on position relative to the midpoint.
        deviation = abs(position - 0.5)
        if deviation <= EQUILIBRIUM_BAND:
            zone = EQUILIBRIUM
            explanation = (
                f'price at {position:.0%} of dealing range — near equilibrium'
            )
        elif position > 0.5:
            zone = PREMIUM
            explanation = (
                f'price at {position:.0%} of dealing range — in premium zone'
            )
        else:
            zone = DISCOUNT
            explanation = (
                f'price at {position:.0%} of dealing range — in discount zone'
            )

        evidence = [EvidenceItem(
            evidence_id=make_evidence_id('premium_discount', midpoint),
            kind='premium_discount',
            direction=(
                'bearish' if zone == PREMIUM
                else 'bullish' if zone == DISCOUNT
                else 'neutral'
            ),
            price=price,
            strength=min(1.0, deviation / 0.5) if zone != EQUILIBRIUM else 0.2,
            source='premium_discount',
            detail=explanation,
        )]

        return PremiumDiscountResult(
            zone=zone,
            range_high=range_high,
            range_low=range_low,
            midpoint=midpoint,
            price_position=round(position, 4),
            explanation=explanation,
            evidence=evidence,
        )
