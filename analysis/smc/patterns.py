"""Pattern Recognition Engine.

Classic chart patterns detected geometrically from the confirmed swing sequence:

  reversal      Double Top · Double Bottom · Head & Shoulders · Inverse H&S
  bilateral     Ascending / Descending / Symmetrical Triangle · Rectangle ·
                Channel · Pennant
  continuation  Bull Flag · Bear Flag · Rising Wedge · Falling Wedge

The engine returns the single best-matching pattern with a direction and a
0–1 confidence. Every pattern is derived from swing geometry — trendline slopes
and swing-height ratios — so the same swings always yield the same pattern.

Confirmation layer only. Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from analysis.structure import BEARISH, BULLISH, Swing

NEUTRAL = 'neutral'

# Two swing prices within this fraction of their mean are "equal".
EQUAL_PCT = 0.02

# A slope flatter than this (normalised per bar, as a fraction of price) is flat.
FLAT_SLOPE = 0.0005


@dataclass(frozen=True)
class Pattern:
    """One detected chart pattern."""

    name: str
    direction: str            # 'bullish' | 'bearish' | 'neutral'
    strength: float           # 0–1
    detail: str


@dataclass
class PatternState:
    """The pattern read for one timeframe."""

    pattern: Optional[Pattern] = None
    candidates: List[Pattern] = field(default_factory=list)
    direction: str = 'neutral'
    score: float = 0.0
    reason: str = 'no_pattern'

    @property
    def name(self) -> str:
        return self.pattern.name if self.pattern else 'None'


def detect_patterns(swings: List[Swing], price: float, atr: float) -> PatternState:
    """Detect the best-matching chart pattern from recent swings.

    Args:
        swings: Confirmed alternating swings, oldest first.
        price: Current price (used for flag/impulse context).
        atr: Current ATR.

    Returns:
        A PatternState with the highest-confidence pattern, or an empty state
        when no recognisable geometry is present.
    """
    if len(swings) < 4:
        return PatternState()

    candidates: List[Pattern] = []
    candidates.extend(_reversal_patterns(swings))
    candidates.extend(_trendline_patterns(swings))
    candidates.extend(_flag_patterns(swings, atr))

    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return PatternState(reason='no recognisable pattern')

    candidates.sort(key=lambda c: c.strength, reverse=True)
    best = candidates[0]
    return PatternState(
        pattern=best,
        candidates=candidates[:3],
        direction=best.direction,
        score=round(best.strength, 4),
        reason=f'{best.name} — {best.detail}',
    )


# ─────────────────────────────────────────────
# Reversal patterns (from the last 3–5 swings)
# ─────────────────────────────────────────────

def _reversal_patterns(swings: List[Swing]) -> List[Pattern]:
    out: List[Pattern] = []
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]

    # ── Double Top / Bottom ──
    if len(highs) >= 2 and _equal(highs[-1].price, highs[-2].price):
        between_low = min(
            (s.price for s in lows if highs[-2].index < s.index < highs[-1].index),
            default=None,
        )
        if between_low is not None:
            out.append(Pattern(
                'Double Top', BEARISH, 0.7,
                f'two highs near {highs[-1].price:.6f}',
            ))
    if len(lows) >= 2 and _equal(lows[-1].price, lows[-2].price):
        between_high = max(
            (s.price for s in highs if lows[-2].index < s.index < lows[-1].index),
            default=None,
        )
        if between_high is not None:
            out.append(Pattern(
                'Double Bottom', BULLISH, 0.7,
                f'two lows near {lows[-1].price:.6f}',
            ))

    # ── Head & Shoulders (3 highs, middle highest, shoulders ~equal) ──
    if len(highs) >= 3:
        left, head, right = highs[-3], highs[-2], highs[-1]
        if head.price > left.price and head.price > right.price and _equal(left.price, right.price, 0.04):
            out.append(Pattern(
                'Head and Shoulders', BEARISH, 0.75,
                f'head {head.price:.6f} above shoulders ~{left.price:.6f}',
            ))
    if len(lows) >= 3:
        left, head, right = lows[-3], lows[-2], lows[-1]
        if head.price < left.price and head.price < right.price and _equal(left.price, right.price, 0.04):
            out.append(Pattern(
                'Inverse Head and Shoulders', BULLISH, 0.75,
                f'head {head.price:.6f} below shoulders ~{left.price:.6f}',
            ))

    return out


# ─────────────────────────────────────────────
# Trendline patterns (from slopes of the recent highs / lows)
# ─────────────────────────────────────────────

def _trendline_patterns(swings: List[Swing]) -> List[Pattern]:
    highs = [s for s in swings if s.is_high][-3:]
    lows = [s for s in swings if not s.is_high][-3:]
    if len(highs) < 2 or len(lows) < 2:
        return []

    high_slope = _slope(highs)
    low_slope = _slope(lows)
    if high_slope is None or low_slope is None:
        return []

    high_flat = abs(high_slope) < FLAT_SLOPE
    low_flat = abs(low_slope) < FLAT_SLOPE
    converging = high_slope < 0 < low_slope

    # ── Triangles ──
    if high_flat and low_slope > FLAT_SLOPE:
        return [Pattern('Ascending Triangle', BULLISH, 0.62, 'flat highs, rising lows')]
    if low_flat and high_slope < -FLAT_SLOPE:
        return [Pattern('Descending Triangle', BEARISH, 0.62, 'flat lows, falling highs')]
    if converging:
        return [Pattern('Symmetrical Triangle', NEUTRAL, 0.5, 'lower highs and higher lows converging')]

    # ── Wedges (both slopes same sign, converging) ──
    if high_slope > FLAT_SLOPE and low_slope > FLAT_SLOPE and low_slope > high_slope:
        return [Pattern('Rising Wedge', BEARISH, 0.58, 'rising and converging — bearish')]
    if high_slope < -FLAT_SLOPE and low_slope < -FLAT_SLOPE and high_slope < low_slope:
        return [Pattern('Falling Wedge', BULLISH, 0.58, 'falling and converging — bullish')]

    # ── Rectangle / Channel ──
    if high_flat and low_flat:
        return [Pattern('Rectangle', NEUTRAL, 0.45, 'flat highs and flat lows')]
    if _parallel(high_slope, low_slope):
        if high_slope > 0:
            return [Pattern('Channel', BULLISH, 0.5, 'parallel rising trendlines')]
        return [Pattern('Channel', BEARISH, 0.5, 'parallel falling trendlines')]

    return []


# ─────────────────────────────────────────────
# Flags & pennants (impulse then counter-consolidation)
# ─────────────────────────────────────────────

def _flag_patterns(swings: List[Swing], atr: float) -> List[Pattern]:
    if len(swings) < 4 or atr <= 0:
        return []

    # The impulse is the largest recent leg; the flag is the drift after it.
    legs = [
        (swings[i], swings[i + 1], abs(swings[i + 1].price - swings[i].price))
        for i in range(len(swings) - 1)
    ]
    impulse = max(legs[:-1], key=lambda leg: leg[2], default=None)
    if impulse is None:
        return []

    start, end, size = impulse
    if size < 2 * atr:
        return []          # no impulse strong enough to fly a flag from

    up_impulse = end.price > start.price
    consolidation = [s for s in swings if s.index > end.index]
    if len(consolidation) < 2:
        return []

    drift = consolidation[-1].price - consolidation[0].price
    drift_size = abs(drift)

    # A flag drifts gently against the impulse; a pennant converges.
    if drift_size < size * 0.5:
        if up_impulse and drift <= 0:
            return [Pattern('Bull Flag', BULLISH, 0.6, 'up impulse then shallow pullback')]
        if not up_impulse and drift >= 0:
            return [Pattern('Bear Flag', BEARISH, 0.6, 'down impulse then shallow bounce')]
        # Converging consolidation after an impulse = pennant.
        return [Pattern(
            'Pennant', BULLISH if up_impulse else BEARISH, 0.5,
            'impulse then converging consolidation',
        )]
    return []


# ─────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────

def _equal(a: float, b: float, tol: float = EQUAL_PCT) -> bool:
    mean = (a + b) / 2.0
    return mean > 0 and abs(a - b) / mean <= tol


def _slope(points: List[Swing]) -> Optional[float]:
    """Least-squares slope of swing prices vs index, normalised by mean price."""
    if len(points) < 2:
        return None
    xs = np.array([p.index for p in points], dtype=float)
    ys = np.array([p.price for p in points], dtype=float)
    mean_price = float(ys.mean())
    if mean_price <= 0:
        return None
    slope = float(np.polyfit(xs, ys, 1)[0])
    return slope / mean_price


def _parallel(slope_a: float, slope_b: float) -> bool:
    """Two non-flat slopes pointing the same way with similar magnitude."""
    if slope_a * slope_b <= 0:
        return False
    ratio = abs(slope_a) / abs(slope_b) if slope_b != 0 else 0
    return 0.5 <= ratio <= 2.0
