"""Diagnostic output for every analysis run (Phase 1).

Records the module-by-module breakdown, each validation gate's pass/fail, and
the final WAIT/BUY/SELL reasoning so that every decision is auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from analysis.confluence import ConfluenceResult
from analysis.generator import TradingSignal
from analysis.scoring import Score


@dataclass
class GateResult:
    """One pass/fail gate in the WAIT cascade."""

    name: str
    passed: bool
    detail: str


@dataclass
class ModuleBreakdown:
    """One module's contribution to the analysis."""

    module: str
    direction: str
    strength: float
    weight: int
    label: str
    detail: str


@dataclass
class IctMsnrDiagnostic:
    """ICT-MSNR diagnostic record (Phase 2A)."""

    msnr_location: str = ''
    msnr_support: Optional[float] = None
    msnr_resistance: Optional[float] = None
    msnr_strength: float = 0.0
    msnr_status: str = ''
    liquidity_direction: str = 'neutral'
    liquidity_sweep: bool = False
    mss_bos: str = ''
    displacement_direction: str = ''
    fvg_direction: str = 'neutral'
    order_block_direction: str = 'neutral'
    premium_discount: str = ''
    confluence_direction: str = ''
    confluence_strength: float = 0.0
    bullish_evidence: List[str] = field(default_factory=list)
    bearish_evidence: List[str] = field(default_factory=list)
    neutral_evidence: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    primary_reason: str = ''
    primary_blocker: Optional[str] = None
    secondary_blockers: List[str] = field(default_factory=list)
    dedup_count: int = 0

    def as_dict(self) -> dict:
        return {
            'msnr': {
                'location': self.msnr_location,
                'support': self.msnr_support,
                'resistance': self.msnr_resistance,
                'strength': round(self.msnr_strength, 3),
                'status': self.msnr_status,
            },
            'ict': {
                'liquidity': self.liquidity_direction,
                'sweep': self.liquidity_sweep,
                'mss_bos': self.mss_bos,
                'displacement': self.displacement_direction,
                'fvg': self.fvg_direction,
                'order_block': self.order_block_direction,
                'premium_discount': self.premium_discount,
            },
            'confluence': {
                'direction': self.confluence_direction,
                'strength': round(self.confluence_strength, 3),
                'bullish_evidence': self.bullish_evidence,
                'bearish_evidence': self.bearish_evidence,
                'neutral_evidence': self.neutral_evidence,
                'conflicts': self.conflicts,
                'primary_reason': self.primary_reason,
                'primary_blocker': self.primary_blocker,
                'secondary_blockers': self.secondary_blockers,
                'dedup_count': self.dedup_count,
            },
        }


@dataclass
class Diagnostic:
    """Complete diagnostic record for one analysis run."""

    symbol: str
    timeframe: str
    direction: str
    modules: List[ModuleBreakdown] = field(default_factory=list)
    gates: List[GateResult] = field(default_factory=list)
    quality: Optional[int] = None
    quality_grade: Optional[str] = None
    confidence: Optional[int] = None
    confidence_grade: Optional[str] = None
    wait_reason: Optional[str] = None
    # Phase 2A: ICT-MSNR diagnostic detail.
    ict_msnr: Optional[IctMsnrDiagnostic] = None

    def as_dict(self) -> dict:
        d = {
            'symbol': self.symbol,
            'timeframe': self.timeframe,
            'direction': self.direction,
            'modules': [
                {
                    'module': m.module,
                    'direction': m.direction,
                    'strength': round(m.strength, 3),
                    'weight': m.weight,
                    'label': m.label,
                    'detail': m.detail,
                }
                for m in self.modules
            ],
            'gates': [
                {'name': g.name, 'passed': g.passed, 'detail': g.detail}
                for g in self.gates
            ],
            'quality': self.quality,
            'quality_grade': self.quality_grade,
            'confidence': self.confidence,
            'confidence_grade': self.confidence_grade,
            'wait_reason': self.wait_reason,
        }
        if self.ict_msnr is not None:
            d['ict_msnr'] = self.ict_msnr.as_dict()
        return d


def build_diagnostic(
    signal: TradingSignal,
    confluence: ConfluenceResult,
    quality: Score,
    confidence: Score,
) -> Diagnostic:
    """Build the diagnostic record from a completed analysis."""
    from analysis.modules import MODULE_WEIGHTS

    modules = [
        ModuleBreakdown(
            module=v.module,
            direction=v.direction,
            strength=v.strength,
            weight=MODULE_WEIGHTS.get(v.module, 0),
            label=v.label,
            detail=v.detail,
        )
        for v in confluence.votes
    ]

    gates = _build_gates(signal, quality, confidence)

    # Phase 2A: ICT-MSNR diagnostic.
    ict_msnr_diag = IctMsnrDiagnostic(
        confluence_direction=confluence.ict_msnr_direction,
        confluence_strength=confluence.ict_msnr_strength,
        conflicts=confluence.ict_msnr_conflicts,
        primary_reason=confluence.ict_msnr_explanation,
    )

    return Diagnostic(
        symbol=signal.symbol,
        timeframe=signal.timeframe,
        direction=signal.direction,
        modules=modules,
        gates=gates,
        quality=quality.value,
        quality_grade=quality.grade,
        confidence=confidence.value,
        confidence_grade=confidence.grade,
        wait_reason=signal.wait_reason,
        ict_msnr=ict_msnr_diag,
    )


def _build_gates(
    signal: TradingSignal, quality: Score, confidence: Score,
) -> List[GateResult]:
    """Reconstruct which gates passed/failed from the signal state."""
    from analysis.scoring import MIN_TRADEABLE_CONFIDENCE, MIN_TRADEABLE_QUALITY

    gates = []
    is_wait = signal.direction == 'WAIT'
    reason = signal.wait_reason or ''

    gates.append(GateResult(
        'market_conditions',
        'market conditions unsuitable' not in reason,
        'clean' if 'market conditions unsuitable' not in reason else reason,
    ))
    gates.append(GateResult(
        'no_conflicts',
        'conflicting signals' not in reason,
        'no hard conflicts' if 'conflicting signals' not in reason else reason,
    ))
    gates.append(GateResult(
        'direction_consensus',
        'no directional consensus' not in reason,
        'direction found' if 'no directional consensus' not in reason else reason,
    ))
    gates.append(GateResult(
        'quality_floor',
        quality.value >= MIN_TRADEABLE_QUALITY or 'setup quality' not in reason,
        f'{quality.value}/100 ({quality.grade})',
    ))
    gates.append(GateResult(
        'confidence_floor',
        confidence.value >= MIN_TRADEABLE_CONFIDENCE or 'engine confidence' not in reason,
        f'{confidence.value}/100 ({confidence.grade})',
    ))
    gates.append(GateResult(
        'valid_stop',
        'no valid stop' not in reason,
        'stop placed' if 'no valid stop' not in reason else reason,
    ))
    gates.append(GateResult(
        'risk_ceiling',
        'risk ceiling' not in reason,
        'within ceiling' if 'risk ceiling' not in reason else reason,
    ))
    gates.append(GateResult(
        'sufficient_targets',
        'not enough structural targets' not in reason,
        'TP ladder built' if 'not enough structural targets' not in reason else reason,
    ))
    gates.append(GateResult(
        'tp1_rr',
        'TP1 reward:risk' not in reason,
        'TP1 R:R adequate' if 'TP1 reward:risk' not in reason else reason,
    ))
    gates.append(GateResult(
        'min_rr',
        'best available reward:risk' not in reason,
        'R:R clears minimum' if 'best available reward:risk' not in reason else reason,
    ))

    return gates
