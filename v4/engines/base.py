"""Shared engine types and the confluence gate (spec §4, §5, §A5).

V4Signal is the engine output DTO. `score_confluence` turns a dict of boolean
confirmations into a count + the list of satisfied factors. At launch, the entry
gate is a CONFLUENCE COUNT (≥ min_confluence of 6) — deliberately NOT an
uncalibrated 0–100 score (spec §A5). The calibrated score / size tilt is a
later-phase upgrade that can be layered on top of this same factor set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


# The six confluence factors evaluated by every engine (spec §4).
CONFLUENCE_FACTORS = (
    'regime_alignment',   # regime is armed for this engine
    'htf_trend',          # trade direction agrees with the higher-timeframe trend
    'structure',          # entry sits at a real market-structure level
    'momentum',           # momentum confirms (DI alignment / reversal / break-hold)
    'volume',             # participation confirms (volume ≥ baseline)
    'volatility_setup',   # volatility context fits the engine (expansion / squeeze)
)


def score_confluence(flags: Dict[str, bool]) -> Tuple[int, List[str]]:
    """Return (count, satisfied_factor_names) over the six confluence factors."""
    parts = [f for f in CONFLUENCE_FACTORS if flags.get(f)]
    return len(parts), parts


@dataclass(frozen=True)
class V4Signal:
    """A validated entry candidate from an engine (pre-sizing, pre-safety)."""

    symbol: str
    side: str                 # 'long' | 'short'
    engine: str               # 'trend' | 'breakout'
    entry: float
    atr: float
    structural_level: float   # anchor for the stop (swing / broken level)
    confluence: int
    confluence_parts: List[str] = field(default_factory=list)
    reason: str = ''
    regime_reason: str = ''
