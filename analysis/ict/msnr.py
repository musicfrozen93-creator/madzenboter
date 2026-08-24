"""MSNR Location Engine — Major Support / Nearest Resistance (Phase 2A).

Identifies where price sits relative to important market levels:

  • Major support and resistance (strongest zones from the existing levels engine)
  • Nearest support and resistance
  • Previous Day High / Low  (PDH / PDL)
  • Previous Week High / Low (PWH / PWL)
  • Important swing highs / lows
  • Repeated reaction levels
  • Distance from important levels (in ATR)

This is a CONTEXTUAL layer.  It does not vote in the Confluence Engine — it
provides location context that the ICT-MSNR Confluence Engine uses to evaluate
the QUALITY of an ICT setup.

Re-uses swings and levels from the existing engines.  No market-data fetch.

Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from analysis.ict.evidence import EvidenceItem, make_evidence_id
from analysis.levels import SupportResistance, Zone
from analysis.structure import Swing

# A level must have this many touches to qualify as "major".
MAJOR_TOUCH_THRESHOLD = 3

# The level is "near" when within this many ATR.
NEAR_ATR = 1.5

# ATR fraction tolerance for identifying repeated reaction levels.
REACTION_TOLERANCE_ATR = 0.3

# Location classifications.
SUPPORT_ZONE = 'SUPPORT_ZONE'
RESISTANCE_ZONE = 'RESISTANCE_ZONE'
MID_RANGE = 'MID_RANGE'
NO_CLEAR_LEVEL = 'NO_CLEAR_LEVEL'
UNAVAILABLE = 'UNAVAILABLE'


@dataclass(frozen=True)
class PeriodicLevel:
    """A time-based level (PDH, PDL, PWH, PWL)."""

    label: str        # 'PDH' | 'PDL' | 'PWH' | 'PWL'
    price: float
    kind: str         # 'high' | 'low'


@dataclass
class MSNRResult:
    """The location read produced by the MSNR engine."""

    location: str = UNAVAILABLE          # SUPPORT_ZONE | RESISTANCE_ZONE | MID_RANGE | NO_CLEAR_LEVEL | UNAVAILABLE
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    major_support: Optional[Zone] = None
    major_resistance: Optional[Zone] = None
    distance_to_support: Optional[float] = None    # in ATR
    distance_to_resistance: Optional[float] = None # in ATR
    periodic_levels: List[PeriodicLevel] = field(default_factory=list)
    important_swing_highs: List[float] = field(default_factory=list)
    important_swing_lows: List[float] = field(default_factory=list)
    repeated_levels: List[float] = field(default_factory=list)
    strength: float = 0.0               # 0–1 how strongly price is at a level
    score: float = 0.0                  # 0–1 contextual quality
    explanation: str = 'insufficient data'

    # Evidence items for deduplication.
    evidence: List[EvidenceItem] = field(default_factory=list)


class MSNREngine:
    """Identifies important market levels and price's position relative to them."""

    def analyze(
        self,
        candles: pd.DataFrame,
        swings: List[Swing],
        levels: SupportResistance,
        price: float,
        atr: float,
    ) -> MSNRResult:
        """Run the full MSNR analysis.

        Args:
            candles: OHLCV data, oldest first.
            swings: Confirmed swings from the structure engine.
            levels: Support/resistance from the existing levels engine.
            price: Current price.
            atr: Current ATR.

        Returns:
            MSNRResult with location classification and all detected levels.
        """
        if candles is None or len(candles) < 10 or price <= 0 or atr <= 0:
            return MSNRResult()

        evidence: List[EvidenceItem] = []

        # 1. Periodic levels (PDH/PDL/PWH/PWL) from candle data.
        periodic = self._detect_periodic_levels(candles)

        # 2. Important swing highs/lows — swings with multiple touches or
        #    very recent formation.
        important_highs = self._important_swings(swings, 'high', atr)
        important_lows = self._important_swings(swings, 'low', atr)

        # 3. Repeated reaction levels — prices where multiple swing points
        #    cluster across BOTH highs and lows (role reversals).
        repeated = self._repeated_reaction_levels(swings, atr)

        # 4. Reuse existing nearest S/R from the levels engine.
        nearest_support = levels.nearest_support.center if levels.nearest_support else None
        nearest_resistance = levels.nearest_resistance.center if levels.nearest_resistance else None

        # 5. Major levels: the highest-strength zones from each side.
        major_support = self._major_zone(levels.supports)
        major_resistance = self._major_zone(levels.resistances)

        # 6. Distances in ATR.
        dist_support = None
        dist_resistance = None
        if nearest_support is not None:
            dist_support = round(abs(price - nearest_support) / atr, 3)
        if nearest_resistance is not None:
            dist_resistance = round(abs(nearest_resistance - price) / atr, 3)

        # 7. Classify the location.
        location, strength, explanation = self._classify_location(
            price, atr, levels, periodic, dist_support, dist_resistance,
        )

        # 8. Build evidence for deduplication.
        if nearest_support is not None:
            evidence.append(EvidenceItem(
                evidence_id=make_evidence_id('support', nearest_support),
                kind='support', direction='bullish', price=nearest_support,
                strength=levels.nearest_support.strength if levels.nearest_support else 0.5,
                source='msnr', detail=f'nearest support at {nearest_support:.8f}',
            ))
        if nearest_resistance is not None:
            evidence.append(EvidenceItem(
                evidence_id=make_evidence_id('resistance', nearest_resistance),
                kind='resistance', direction='bearish', price=nearest_resistance,
                strength=levels.nearest_resistance.strength if levels.nearest_resistance else 0.5,
                source='msnr', detail=f'nearest resistance at {nearest_resistance:.8f}',
            ))
        for p in periodic:
            direction = 'bullish' if p.kind == 'low' else 'bearish'
            evidence.append(EvidenceItem(
                evidence_id=make_evidence_id(f'periodic_{p.label}', p.price),
                kind=p.label.lower(), direction=direction, price=p.price,
                strength=0.5, source='msnr',
                detail=f'{p.label} at {p.price:.8f}',
            ))

        # Score: how relevant the location context is.
        score = self._compute_score(location, strength, dist_support, dist_resistance)

        return MSNRResult(
            location=location,
            nearest_support=nearest_support,
            nearest_resistance=nearest_resistance,
            major_support=major_support,
            major_resistance=major_resistance,
            distance_to_support=dist_support,
            distance_to_resistance=dist_resistance,
            periodic_levels=periodic,
            important_swing_highs=important_highs,
            important_swing_lows=important_lows,
            repeated_levels=repeated,
            strength=round(strength, 4),
            score=round(score, 4),
            explanation=explanation,
            evidence=evidence,
        )

    # ── Internals ──

    def _detect_periodic_levels(self, df: pd.DataFrame) -> List[PeriodicLevel]:
        """Detect PDH/PDL/PWH/PWL from candle timestamps.

        If timestamps are not timezone-aware or the series is too short,
        we fall back to using candle index windows.
        """
        levels: List[PeriodicLevel] = []
        n = len(df)

        # Heuristic: use the last ~24 bars before the final bar for "previous day"
        # and the last ~120 bars for "previous week" on a 15m chart.
        # This is approximate — the timeframe is not always known, but the
        # important thing is we identify recent session highs/lows.
        if n >= 50:
            # "Previous day" — use bars [-48:-1] (roughly 24 hours on 30m candles)
            day_window = min(48, n - 1)
            prev_day = df.iloc[-(day_window + 1):-1]
            pdh = float(prev_day['high'].max())
            pdl = float(prev_day['low'].min())
            levels.append(PeriodicLevel('PDH', pdh, 'high'))
            levels.append(PeriodicLevel('PDL', pdl, 'low'))

        if n >= 200:
            # "Previous week" — use bars [-240:-48] (roughly one week on 30m)
            week_end = min(48, n - 1)
            week_start = min(240, n - 1)
            prev_week = df.iloc[-week_start:-week_end]
            if len(prev_week) >= 10:
                pwh = float(prev_week['high'].max())
                pwl = float(prev_week['low'].min())
                levels.append(PeriodicLevel('PWH', pwh, 'high'))
                levels.append(PeriodicLevel('PWL', pwl, 'low'))

        return levels

    def _important_swings(
        self, swings: List[Swing], kind: str, atr: float, top_n: int = 5
    ) -> List[float]:
        """The most significant swing points of a given kind.

        Significance is measured by how extreme the price is relative to
        recent history — the outer swings define the dealing range.
        """
        filtered = [s for s in swings if s.kind == kind]
        if not filtered:
            return []
        # Sort by price: highest for highs, lowest for lows.
        if kind == 'high':
            filtered.sort(key=lambda s: s.price, reverse=True)
        else:
            filtered.sort(key=lambda s: s.price)
        return [s.price for s in filtered[:top_n]]

    def _repeated_reaction_levels(
        self, swings: List[Swing], atr: float
    ) -> List[float]:
        """Levels where BOTH swing highs and swing lows cluster — role reversals.

        A price that acted as support and later as resistance (or vice versa)
        is a particularly significant level.
        """
        if len(swings) < 4 or atr <= 0:
            return []

        tolerance = REACTION_TOLERANCE_ATR * atr
        highs = sorted([s.price for s in swings if s.is_high])
        lows = sorted([s.price for s in swings if not s.is_high])

        repeated: List[float] = []
        for h in highs:
            for l in lows:
                if abs(h - l) <= tolerance:
                    avg = (h + l) / 2.0
                    # Avoid duplicating already-found levels.
                    if not any(abs(avg - r) <= tolerance for r in repeated):
                        repeated.append(avg)
        return repeated

    def _major_zone(self, zones: List[Zone]) -> Optional[Zone]:
        """The strongest zone with enough touches to qualify as "major"."""
        for zone in zones:
            if zone.touches >= MAJOR_TOUCH_THRESHOLD:
                return zone
        # Fall back to strongest regardless.
        return zones[0] if zones else None

    def _classify_location(
        self,
        price: float,
        atr: float,
        levels: SupportResistance,
        periodic: List[PeriodicLevel],
        dist_support: Optional[float],
        dist_resistance: Optional[float],
    ) -> tuple[str, float, str]:
        """Classify where price sits relative to the detected levels.

        Returns:
            (location, strength 0–1, explanation)
        """
        at_support = (
            levels.nearest_support is not None
            and levels.nearest_support.contains(price)
        )
        near_support = dist_support is not None and dist_support <= NEAR_ATR
        at_resistance = (
            levels.nearest_resistance is not None
            and levels.nearest_resistance.contains(price)
        )
        near_resistance = dist_resistance is not None and dist_resistance <= NEAR_ATR

        # Check periodic levels.
        at_periodic_support = any(
            p.kind == 'low' and abs(price - p.price) / atr <= NEAR_ATR
            for p in periodic
        )
        at_periodic_resistance = any(
            p.kind == 'high' and abs(price - p.price) / atr <= NEAR_ATR
            for p in periodic
        )

        if at_support:
            strength = levels.nearest_support.strength
            return SUPPORT_ZONE, strength, (
                f'price is AT support zone ({levels.nearest_support.touches} touches)'
            )
        if at_resistance:
            strength = levels.nearest_resistance.strength
            return RESISTANCE_ZONE, strength, (
                f'price is AT resistance zone ({levels.nearest_resistance.touches} touches)'
            )
        if near_support and not near_resistance:
            strength = levels.nearest_support.strength * 0.7 if levels.nearest_support else 0.5
            extra = ' + periodic support' if at_periodic_support else ''
            return SUPPORT_ZONE, strength, (
                f'price is near support ({dist_support:.1f} ATR){extra}'
            )
        if near_resistance and not near_support:
            strength = levels.nearest_resistance.strength * 0.7 if levels.nearest_resistance else 0.5
            extra = ' + periodic resistance' if at_periodic_resistance else ''
            return RESISTANCE_ZONE, strength, (
                f'price is near resistance ({dist_resistance:.1f} ATR){extra}'
            )
        if near_support and near_resistance:
            return MID_RANGE, 0.3, 'price is between nearby support and resistance'
        if dist_support is not None or dist_resistance is not None:
            return MID_RANGE, 0.2, 'price is in the middle of the range'

        return NO_CLEAR_LEVEL, 0.0, 'no clear support or resistance nearby'

    def _compute_score(
        self,
        location: str,
        strength: float,
        dist_support: Optional[float],
        dist_resistance: Optional[float],
    ) -> float:
        """0–1 contextual quality of the location read.

        High score = price is at a strong, well-defined level.
        Low score = price is mid-range or levels are unclear.
        """
        if location == UNAVAILABLE:
            return 0.0
        if location == NO_CLEAR_LEVEL:
            return 0.1

        # Location quality starts from the zone strength.
        base = strength

        # Proximity bonus: the closer to a level, the more relevant.
        proximity = 0.0
        if location == SUPPORT_ZONE and dist_support is not None:
            proximity = max(0.0, 1.0 - dist_support / NEAR_ATR)
        elif location == RESISTANCE_ZONE and dist_resistance is not None:
            proximity = max(0.0, 1.0 - dist_resistance / NEAR_ATR)

        return min(1.0, 0.6 * base + 0.4 * proximity)
