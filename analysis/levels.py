"""Support and Resistance — swing clustering into price zones.

Swing points that repeat around the same price are one level, not several. This
module clusters confirmed swings into zones, scores each zone by how many times
price respected it and how recently, and reports the nearest zone above and
below the current price.

Zone strength (0–1) combines:
  • touches  — how many swings formed there (the dominant factor)
  • recency  — how recently the level was last respected
  • tightness— how narrow the cluster is relative to ATR

Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from analysis.structure import Swing

# A cluster's half-width: the larger of this ATR fraction and price fraction.
CLUSTER_ATR_FRACTION = 0.5
CLUSTER_PRICE_FRACTION = 0.002      # 0.2%

# Touches at or above this count saturate the touch component of strength.
TOUCH_SATURATION = 4

# A zone counts as "near" price when it sits within this many ATR of it.
NEAR_ATR_MULTIPLE = 1.5


@dataclass(frozen=True)
class Zone:
    """A horizontal price zone formed by clustered swings."""

    center: float
    lower: float
    upper: float
    touches: int
    strength: float          # 0–1
    kind: str                # 'support' | 'resistance'
    last_touch_index: int

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def distance_from(self, price: float) -> float:
        """Absolute distance from a price to the nearest edge of the zone."""
        if self.contains(price):
            return 0.0
        return min(abs(price - self.lower), abs(price - self.upper))


@dataclass
class SupportResistance:
    """All detected zones plus the ones that matter right now."""

    supports: List[Zone] = field(default_factory=list)
    resistances: List[Zone] = field(default_factory=list)
    nearest_support: Optional[Zone] = None
    nearest_resistance: Optional[Zone] = None
    price: float = 0.0
    reason: str = 'no_levels_detected'

    @property
    def has_levels(self) -> bool:
        return bool(self.supports or self.resistances)

    def room_to_resistance(self) -> Optional[float]:
        """Distance from price up to the nearest resistance, if any."""
        if self.nearest_resistance is None:
            return None
        return self.nearest_resistance.center - self.price

    def room_to_support(self) -> Optional[float]:
        """Distance from price down to the nearest support, if any."""
        if self.nearest_support is None:
            return None
        return self.price - self.nearest_support.center

    def position_label(self, atr: float) -> str:
        """Where price sits relative to its nearest levels, in plain words."""
        if not self.has_levels:
            return 'no defined levels'
        if self.nearest_support and self.nearest_support.contains(self.price):
            return 'at support'
        if self.nearest_resistance and self.nearest_resistance.contains(self.price):
            return 'at resistance'
        near = NEAR_ATR_MULTIPLE * atr if atr > 0 else 0.0
        if self.nearest_support and self.price - self.nearest_support.upper <= near:
            return 'just above support'
        if self.nearest_resistance and self.nearest_resistance.lower - self.price <= near:
            return 'just below resistance'
        return 'mid-range'


def detect_levels(
    swings: List[Swing], price: float, atr: float, total_bars: int
) -> SupportResistance:
    """Cluster swings into support and resistance zones.

    Args:
        swings: Confirmed swings from `analysis.structure.find_swings`.
        price: Current price, used to split zones into support vs resistance.
        atr: Current ATR, sets the clustering tolerance.
        total_bars: Length of the analysed series, used for the recency weight.

    Returns:
        A SupportResistance with zones sorted by strength (strongest first).
    """
    if not swings or price <= 0:
        return SupportResistance(price=price)

    tolerance = max(CLUSTER_ATR_FRACTION * atr, CLUSTER_PRICE_FRACTION * price)
    if tolerance <= 0:
        return SupportResistance(price=price)

    clusters = _cluster(swings, tolerance)
    zones: List[Zone] = []
    for members in clusters:
        prices = [s.price for s in members]
        center = sum(prices) / len(prices)
        lower, upper = min(prices), max(prices)
        last_index = max(s.index for s in members)
        zones.append(Zone(
            center=center,
            # A single-touch level still needs width to be usable as a zone.
            lower=min(lower, center - tolerance / 2),
            upper=max(upper, center + tolerance / 2),
            touches=len(members),
            strength=_strength(members, upper - lower, atr, last_index, total_bars),
            kind='support' if center < price else 'resistance',
            last_touch_index=last_index,
        ))

    supports = sorted(
        (z for z in zones if z.kind == 'support'), key=lambda z: z.strength, reverse=True
    )
    resistances = sorted(
        (z for z in zones if z.kind == 'resistance'), key=lambda z: z.strength, reverse=True
    )

    nearest_support = min(
        (z for z in zones if z.kind == 'support'),
        key=lambda z: price - z.center, default=None,
    )
    nearest_resistance = min(
        (z for z in zones if z.kind == 'resistance'),
        key=lambda z: z.center - price, default=None,
    )

    return SupportResistance(
        supports=supports,
        resistances=resistances,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        price=price,
        reason=f'{len(supports)} support / {len(resistances)} resistance zones',
    )


def _cluster(swings: List[Swing], tolerance: float) -> List[List[Swing]]:
    """Greedily group swings whose prices sit within `tolerance` of each other."""
    ordered = sorted(swings, key=lambda s: s.price)
    clusters: List[List[Swing]] = []
    current: List[Swing] = [ordered[0]]

    for swing in ordered[1:]:
        # Compare against the cluster's running centre, not just its last member,
        # so a slow drift cannot chain unrelated levels into one giant cluster.
        centre = sum(s.price for s in current) / len(current)
        if abs(swing.price - centre) <= tolerance:
            current.append(swing)
        else:
            clusters.append(current)
            current = [swing]
    clusters.append(current)
    return clusters


def _strength(
    members: List[Swing], width: float, atr: float, last_index: int, total_bars: int
) -> float:
    """Score a zone 0–1 from touch count, recency, and tightness."""
    # Touches: the primary signal. Saturates so a very old busy level cannot
    # outrank a clean recent one purely on count.
    touch_score = min(1.0, len(members) / TOUCH_SATURATION)

    # Recency: a level last respected 5 bars ago matters more than 500 ago.
    recency_score = (last_index / total_bars) if total_bars > 0 else 0.0

    # Tightness: a zone wider than 1 ATR is a region, not a level.
    if atr > 0:
        tightness_score = max(0.0, 1.0 - (width / atr))
    else:
        tightness_score = 1.0 if width == 0 else 0.0

    return round(
        0.55 * touch_score + 0.30 * recency_score + 0.15 * tightness_score, 4
    )
