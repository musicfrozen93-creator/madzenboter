"""Displacement Engine — directional impulsive movement (Phase 2A).

Displacement is NOT just "a large candle".  It is a meaningful directional
movement characterised by:

  1. A candle body significantly larger than recent average bodies.
  2. Range expansion relative to recent ATR.
  3. A clear directional close (close far from the wick that opposes the move).
  4. Ideally accompanied by a structural break (BOS/MSS).
  5. Volume confirmation when available.

Returns BULLISH, BEARISH, NEUTRAL, or UNAVAILABLE.

Confirmation layer only.  Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from analysis.ict.evidence import EvidenceItem, make_evidence_id

# A body must be this many times the average body to count as expanded.
BODY_EXPANSION_THRESHOLD = 1.8

# Range must expand by at least this factor relative to ATR.
RANGE_ATR_THRESHOLD = 1.2

# The close must sit in the directional third of the candle's range.
DIRECTIONAL_CLOSE_RATIO = 0.33

# How many candles to look back for average body/range calculation.
LOOKBACK = 10

BULLISH = 'BULLISH'
BEARISH = 'BEARISH'
NEUTRAL = 'NEUTRAL'
UNAVAILABLE = 'UNAVAILABLE'


@dataclass(frozen=True)
class DisplacementCandle:
    """One candle identified as a displacement."""

    index: int
    direction: str         # 'BULLISH' | 'BEARISH'
    body_ratio: float      # body size / average body
    range_atr: float       # candle range / ATR
    close_position: float  # 0=bottom, 1=top of the candle
    volume_ratio: float    # volume / average volume (0 if unavailable)


@dataclass
class DisplacementResult:
    """The displacement read for one timeframe."""

    direction: str = UNAVAILABLE        # BULLISH | BEARISH | NEUTRAL | UNAVAILABLE
    strength: float = 0.0               # 0–1
    candles: List[DisplacementCandle] = field(default_factory=list)
    has_structure_break: bool = False    # displacement accompanied by BOS/MSS
    explanation: str = 'insufficient data'

    # Evidence for deduplication.
    evidence: List[EvidenceItem] = field(default_factory=list)


class DisplacementEngine:
    """Detects meaningful directional displacement from OHLCV data."""

    def analyze(
        self,
        df: pd.DataFrame,
        atr: float,
        structure_event: Optional[str] = None,
        structure_event_direction: Optional[str] = None,
    ) -> DisplacementResult:
        """Detect displacement in the most recent candles.

        Args:
            df: OHLCV candles, oldest first.
            atr: Current ATR.
            structure_event: 'bos' | 'choch' | None from the structure engine.
            structure_event_direction: 'bullish' | 'bearish' | None.

        Returns:
            DisplacementResult with direction and strength.
        """
        if df is None or len(df) < LOOKBACK + 3 or atr <= 0:
            return DisplacementResult()

        opens = df['open'].to_numpy(dtype=float)
        highs = df['high'].to_numpy(dtype=float)
        lows = df['low'].to_numpy(dtype=float)
        closes = df['close'].to_numpy(dtype=float)
        volumes = df['volume'].to_numpy(dtype=float)
        n = len(df)

        # Average body and range over the lookback window.
        recent = slice(-(LOOKBACK + 1), -1)
        bodies = np.abs(closes[recent] - opens[recent])
        avg_body = float(bodies.mean()) if len(bodies) > 0 else 0.0
        avg_volume = float(volumes[recent].mean()) if len(volumes[recent]) > 0 else 0.0

        if avg_body <= 0:
            return DisplacementResult(
                direction=NEUTRAL, explanation='candle bodies too small to measure'
            )

        # Scan the last 3 candles for displacement.
        detected: List[DisplacementCandle] = []
        for i in range(max(LOOKBACK, n - 3), n):
            body = abs(closes[i] - opens[i])
            candle_range = highs[i] - lows[i]
            if candle_range <= 0:
                continue

            body_ratio = body / avg_body
            range_atr = candle_range / atr

            # Close position: 0 = at low, 1 = at high.
            close_pos = (closes[i] - lows[i]) / candle_range

            # Volume ratio.
            vol_ratio = (volumes[i] / avg_volume) if avg_volume > 0 else 0.0

            # Check the displacement criteria.
            body_expanded = body_ratio >= BODY_EXPANSION_THRESHOLD
            range_expanded = range_atr >= RANGE_ATR_THRESHOLD

            if not (body_expanded and range_expanded):
                continue

            # Determine direction from the close position.
            is_bullish = (
                closes[i] > opens[i]
                and close_pos >= (1.0 - DIRECTIONAL_CLOSE_RATIO)
            )
            is_bearish = (
                closes[i] < opens[i]
                and close_pos <= DIRECTIONAL_CLOSE_RATIO
            )

            if not (is_bullish or is_bearish):
                continue

            direction = BULLISH if is_bullish else BEARISH
            detected.append(DisplacementCandle(
                index=i, direction=direction,
                body_ratio=round(body_ratio, 3),
                range_atr=round(range_atr, 3),
                close_position=round(close_pos, 3),
                volume_ratio=round(vol_ratio, 3),
            ))

        if not detected:
            return DisplacementResult(
                direction=NEUTRAL, explanation='no displacement detected in recent candles'
            )

        # Use the most recent displacement candle.
        latest = detected[-1]

        # Check if a structure break accompanies the displacement.
        has_break = (
            structure_event in ('bos', 'choch')
            and structure_event_direction is not None
        )
        break_aligns = (
            has_break
            and (
                (latest.direction == BULLISH and structure_event_direction == 'bullish')
                or (latest.direction == BEARISH and structure_event_direction == 'bearish')
            )
        )

        # Strength calculation.
        body_score = min(1.0, (latest.body_ratio - 1.0) / 3.0)
        range_score = min(1.0, (latest.range_atr - 0.5) / 2.0)
        close_score = 1.0 - abs(
            latest.close_position - (1.0 if latest.direction == BULLISH else 0.0)
        )
        vol_score = min(1.0, latest.volume_ratio / 2.0) if latest.volume_ratio > 0 else 0.5
        break_bonus = 0.15 if break_aligns else 0.0

        strength = min(1.0,
            0.30 * body_score
            + 0.25 * range_score
            + 0.20 * close_score
            + 0.10 * vol_score
            + break_bonus
        )

        # Build evidence.
        evidence = [EvidenceItem(
            evidence_id=make_evidence_id('displacement', float(closes[latest.index])),
            kind='displacement',
            direction='bullish' if latest.direction == BULLISH else 'bearish',
            price=float(closes[latest.index]),
            strength=strength,
            source='displacement',
            detail=(
                f'{latest.direction} displacement: '
                f'{latest.body_ratio:.1f}x body, {latest.range_atr:.1f}x ATR range'
                f'{", with structure break" if break_aligns else ""}'
            ),
        )]

        return DisplacementResult(
            direction=latest.direction,
            strength=round(strength, 4),
            candles=detected,
            has_structure_break=break_aligns,
            explanation=(
                f'{latest.direction} displacement on candle {latest.index}: '
                f'{latest.body_ratio:.1f}x avg body, {latest.range_atr:.1f}x ATR range, '
                f'close at {latest.close_position:.0%} of range'
                f'{", confirmed by " + structure_event.upper() if break_aligns else ""}'
            ),
            evidence=evidence,
        )
