"""ICT Structure Adapter — ICT interpretation of existing structure (Phase 2A).

This is NOT a second structure engine.  The existing ``analysis.structure``
module detects swings, BOS, and CHoCH.  This adapter REINTERPRETS those results
through an ICT lens:

  • BOS (Break of Structure):  an existing BOS that continues the prevailing
    trend.  ICT treats this as confirmation of the current move.
  • MSS (Market Structure Shift):  what the existing engine calls CHoCH — a
    break AGAINST the prevailing structure.  ICT treats this as a potential
    reversal signal.
  • Higher-High / Higher-Low / Lower-High / Lower-Low:  already computed by
    the structure engine; this adapter exposes them with ICT naming.

It produces evidence items with THE SAME evidence_ids as the existing
structure engine would, so the deduplication layer merges them automatically.

Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from analysis.ict.evidence import EvidenceItem, make_evidence_id
from analysis.structure import (
    BEARISH,
    BOS,
    BULLISH,
    CHOCH,
    RANGE,
    StructureState,
)


@dataclass
class ICTStructureResult:
    """ICT interpretation of the existing market structure."""

    # ICT-specific labels for structure events.
    has_bos: bool = False
    bos_direction: Optional[str] = None        # 'bullish' | 'bearish'
    has_mss: bool = False
    mss_direction: Optional[str] = None        # 'bullish' | 'bearish'
    has_displacement: bool = False              # from the caller, not detected here

    # HH/HL/LH/LL labels — directly from the structure engine.
    higher_highs: bool = False
    higher_lows: bool = False
    lower_highs: bool = False
    lower_lows: bool = False

    # Derived ICT readings.
    trend: str = RANGE                         # 'bullish' | 'bearish' | 'range'
    strength: float = 0.0                      # 0–1
    explanation: str = 'no structure data'

    # Evidence for deduplication.
    evidence: List[EvidenceItem] = field(default_factory=list)


class ICTStructureAdapter:
    """Adapts the existing structure engine output to ICT terminology."""

    def interpret(
        self,
        structure: StructureState,
    ) -> ICTStructureResult:
        """Interpret existing structure results through an ICT lens.

        This does NOT recompute swings or breaks.  It reads the
        ``StructureState`` and translates the events into ICT concepts.

        Args:
            structure: The structure result from ``analysis.structure``.

        Returns:
            ICTStructureResult with ICT-labelled events and evidence.
        """
        if not structure.has_structure:
            return ICTStructureResult()

        evidence: List[EvidenceItem] = []

        # Map existing BOS/CHoCH to ICT BOS/MSS.
        has_bos = structure.event == BOS
        bos_direction = structure.event_direction if has_bos else None
        has_mss = structure.event == CHOCH
        mss_direction = structure.event_direction if has_mss else None

        # Build evidence.  Use the SAME evidence_id formula as the
        # existing structure would, keyed on the break price so that
        # the structure event is counted once, not twice.
        if has_bos and bos_direction:
            break_price = (
                structure.last_swing_high
                if bos_direction == BULLISH
                else structure.last_swing_low
            )
            if break_price is not None:
                evidence.append(EvidenceItem(
                    evidence_id=make_evidence_id('bos', break_price),
                    kind='bos',
                    direction=bos_direction,
                    price=break_price,
                    strength=min(1.0, structure.event_strength / 2.0),
                    source='ict_structure',
                    detail=f'ICT BOS {bos_direction} at {break_price:.8f}',
                ))

        if has_mss and mss_direction:
            break_price = (
                structure.last_swing_high
                if mss_direction == BULLISH
                else structure.last_swing_low
            )
            if break_price is not None:
                evidence.append(EvidenceItem(
                    evidence_id=make_evidence_id('mss', break_price),
                    kind='mss',
                    direction=mss_direction,
                    price=break_price,
                    strength=min(1.0, structure.event_strength / 2.0),
                    source='ict_structure',
                    detail=f'ICT MSS (Market Structure Shift) {mss_direction} at {break_price:.8f}',
                ))

        # Swing-level evidence — same ids as the existing swing highs/lows
        # so they merge with MSNR and Liquidity evidence.
        if structure.last_swing_high is not None:
            evidence.append(EvidenceItem(
                evidence_id=make_evidence_id('swing_high', structure.last_swing_high),
                kind='swing_high',
                direction='bearish',  # a swing high is overhead resistance
                price=structure.last_swing_high,
                strength=0.4,
                source='ict_structure',
                detail=f'swing high at {structure.last_swing_high:.8f}',
            ))
        if structure.last_swing_low is not None:
            evidence.append(EvidenceItem(
                evidence_id=make_evidence_id('swing_low', structure.last_swing_low),
                kind='swing_low',
                direction='bullish',  # a swing low is underlying support
                price=structure.last_swing_low,
                strength=0.4,
                source='ict_structure',
                detail=f'swing low at {structure.last_swing_low:.8f}',
            ))

        # Explanation.
        parts: List[str] = []
        if has_bos:
            parts.append(f'BOS {bos_direction}')
        if has_mss:
            parts.append(f'MSS {mss_direction}')
        if structure.higher_highs:
            parts.append('HH')
        if structure.higher_lows:
            parts.append('HL')
        if structure.lower_highs:
            parts.append('LH')
        if structure.lower_lows:
            parts.append('LL')
        explanation = ', '.join(parts) if parts else structure.reason

        return ICTStructureResult(
            has_bos=has_bos,
            bos_direction=bos_direction,
            has_mss=has_mss,
            mss_direction=mss_direction,
            higher_highs=structure.higher_highs,
            higher_lows=structure.higher_lows,
            lower_highs=structure.lower_highs,
            lower_lows=structure.lower_lows,
            trend=structure.trend,
            strength=structure.strength,
            explanation=explanation,
            evidence=evidence,
        )
