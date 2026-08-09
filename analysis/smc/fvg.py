"""Fair Value Gap Engine (Smart Money Concepts).

A fair value gap (FVG) is a three-candle imbalance where price moved so fast it
left a gap the market tends to revisit:

  • Bullish FVG: candle[i-1].high < candle[i+1].low  (a gap below current price
    that acts as support)
  • Bearish FVG: candle[i-1].low  > candle[i+1].high (a gap above that acts as
    resistance)

A gap is FILLED once later price trades back through it, UNFILLED otherwise.

Confirmation layer only. Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from analysis.structure import BEARISH, BULLISH

# A gap smaller than this fraction of ATR is noise, not an imbalance.
MIN_GAP_ATR = 0.15

FILLED = 'filled'
UNFILLED = 'unfilled'


@dataclass(frozen=True)
class FairValueGap:
    """One detected fair value gap."""

    direction: str            # 'bullish' | 'bearish'
    top: float
    bottom: float
    index: int                # middle candle of the three
    state: str                # 'filled' | 'unfilled'
    size: float               # absolute gap height
    size_atr: float           # gap height in ATR

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    def distance_atr(self, price: float, atr: float) -> float:
        if atr <= 0:
            return 0.0
        if self.bottom <= price <= self.top:
            return 0.0
        return min(abs(price - self.top), abs(price - self.bottom)) / atr


@dataclass
class FairValueGapState:
    """The FVG read for one timeframe."""

    gaps: List[FairValueGap] = field(default_factory=list)
    nearest: Optional[FairValueGap] = None
    direction: str = 'neutral'
    score: float = 0.0
    distance_atr: float = 0.0
    reason: str = 'no_fair_value_gaps'

    @property
    def unfilled(self) -> List[FairValueGap]:
        return [g for g in self.gaps if g.state == UNFILLED]


def detect_fair_value_gaps(
    df: pd.DataFrame, atr: float, max_gaps: int = 12
) -> FairValueGapState:
    """Detect fair value gaps and classify the nearest UNFILLED one.

    Args:
        df: OHLCV candles, oldest first.
        atr: Current ATR, sets the minimum meaningful gap and the distance scale.

    Returns:
        A FairValueGapState. `direction`/`score` describe the nearest unfilled
        gap — the imbalance price is most likely to be drawn toward.
    """
    if df is None or len(df) < 3 or atr <= 0:
        return FairValueGapState()

    highs = df['high'].to_numpy(dtype=float)
    lows = df['low'].to_numpy(dtype=float)
    closes = df['close'].to_numpy(dtype=float)
    n = len(df)
    price = float(closes[-1])
    min_gap = MIN_GAP_ATR * atr

    gaps: List[FairValueGap] = []
    for i in range(1, n - 1):
        # Bullish: gap between candle i-1 high and candle i+1 low.
        if lows[i + 1] - highs[i - 1] >= min_gap:
            bottom, top = float(highs[i - 1]), float(lows[i + 1])
            state = _bullish_state(lows, i + 2, bottom)
            gaps.append(_build(BULLISH, top, bottom, i, state, atr))
        # Bearish: gap between candle i-1 low and candle i+1 high.
        elif lows[i - 1] - highs[i + 1] >= min_gap:
            bottom, top = float(highs[i + 1]), float(lows[i - 1])
            state = _bearish_state(highs, i + 2, top)
            gaps.append(_build(BEARISH, top, bottom, i, state, atr))

    gaps.sort(key=lambda g: g.index, reverse=True)
    gaps = gaps[:max_gaps]

    unfilled = [g for g in gaps if g.state == UNFILLED]
    nearest = min(
        unfilled, key=lambda g: g.distance_atr(price, atr), default=None
    )

    if nearest is None:
        return FairValueGapState(
            gaps=gaps,
            reason='fair value gaps found but all filled' if gaps else 'no_fair_value_gaps',
        )

    distance = nearest.distance_atr(price, atr)
    proximity = max(0.0, 1.0 - min(1.0, distance / 2.0))
    size_score = min(1.0, nearest.size_atr / 1.0)
    score = round((0.6 * proximity + 0.4 * size_score), 4)

    return FairValueGapState(
        gaps=gaps,
        nearest=nearest,
        direction=nearest.direction,
        score=score,
        distance_atr=round(distance, 3),
        reason=(
            f'unfilled {nearest.direction} FVG {nearest.size_atr:.2f} ATR wide, '
            f'{distance:.1f} ATR from price'
        ),
    )


def _bullish_state(lows, start: int, bottom: float) -> str:
    """A bullish gap is filled once price trades back down into it."""
    if start >= len(lows):
        return UNFILLED
    return FILLED if float(lows[start:].min()) <= bottom else UNFILLED


def _bearish_state(highs, start: int, top: float) -> str:
    """A bearish gap is filled once price trades back up into it."""
    if start >= len(highs):
        return UNFILLED
    return FILLED if float(highs[start:].max()) >= top else UNFILLED


def _build(direction, top, bottom, index, state, atr) -> FairValueGap:
    size = abs(top - bottom)
    return FairValueGap(
        direction=direction, top=max(top, bottom), bottom=min(top, bottom),
        index=index, state=state, size=round(size, 10),
        size_atr=round(size / atr, 3) if atr > 0 else 0.0,
    )
