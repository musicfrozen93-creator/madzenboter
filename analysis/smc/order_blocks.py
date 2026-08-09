"""Order Block Engine (Smart Money Concepts).

An order block is the last opposing candle before an impulsive, structure-breaking
move — the footprint of institutional orders. A bullish order block is the last
down candle before an up-move that takes out a swing high; a bearish order block
is the mirror.

Lifecycle:
  • fresh        price has not returned to the block since it formed
  • mitigated    price has tapped into the block but it still holds
  • invalidated  price has closed decisively through the block

This is a CONFIRMATION layer. It never creates a signal on its own — it nudges
the confluence one way or the other via a directional vote.

Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from analysis.structure import BEARISH, BULLISH, Swing

# A move counts as a displacement (impulse) when it travels at least this many
# ATR beyond the order-block candle.
DISPLACEMENT_ATR = 1.0

# The impulse must break a recent swing for the block to be structurally valid.
BREAK_LOOKAHEAD = 5

# How many candles after the block to look for the displacement.
IMPULSE_WINDOW = 3

FRESH = 'fresh'
MITIGATED = 'mitigated'
INVALIDATED = 'invalidated'


@dataclass(frozen=True)
class OrderBlock:
    """One detected order block."""

    direction: str            # 'bullish' | 'bearish'
    top: float
    bottom: float
    index: int                # candle index where it formed
    state: str                # 'fresh' | 'mitigated' | 'invalidated'
    displacement_atr: float   # size of the impulse that created it, in ATR
    volume_ratio: float       # forming candle volume vs recent average
    strength: float           # 0–1

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    def distance_atr(self, price: float, atr: float) -> float:
        """Distance from price to the nearest edge of the block, in ATR."""
        if atr <= 0:
            return 0.0
        if self.bottom <= price <= self.top:
            return 0.0
        return min(abs(price - self.top), abs(price - self.bottom)) / atr


@dataclass
class OrderBlockState:
    """The order-block read for one timeframe."""

    blocks: List[OrderBlock] = field(default_factory=list)
    nearest: Optional[OrderBlock] = None
    direction: str = 'neutral'          # lean of the nearest fresh block
    score: float = 0.0                  # 0–1 confirmation strength
    distance_atr: float = 0.0
    reason: str = 'no_order_blocks'

    @property
    def fresh(self) -> List[OrderBlock]:
        return [b for b in self.blocks if b.state == FRESH]

    @property
    def mitigated(self) -> List[OrderBlock]:
        return [b for b in self.blocks if b.state == MITIGATED]


def detect_order_blocks(
    df: pd.DataFrame, swings: List[Swing], atr: float, max_blocks: int = 8
) -> OrderBlockState:
    """Detect order blocks and classify the nearest by proximity to price.

    Args:
        df: OHLCV candles, oldest first.
        swings: Confirmed swings, used to require a structural break.
        atr: Current ATR, the yardstick for displacement and distance.

    Returns:
        An OrderBlockState. `direction`/`score` describe the nearest FRESH block
        (the one price is most likely to react to).
    """
    if df is None or len(df) < IMPULSE_WINDOW + 2 or atr <= 0:
        return OrderBlockState()

    opens = df['open'].to_numpy(dtype=float)
    highs = df['high'].to_numpy(dtype=float)
    lows = df['low'].to_numpy(dtype=float)
    closes = df['close'].to_numpy(dtype=float)
    volumes = df['volume'].to_numpy(dtype=float)
    n = len(df)
    price = float(closes[-1])

    swing_highs = [s.price for s in swings if s.is_high]
    swing_lows = [s.price for s in swings if not s.is_high]

    blocks: List[OrderBlock] = []
    for i in range(1, n - IMPULSE_WINDOW - 1):
        candle_down = closes[i] < opens[i]
        candle_up = closes[i] > opens[i]

        window = slice(i + 1, i + 1 + IMPULSE_WINDOW)
        move_up = float(highs[window].max()) - float(highs[i])
        move_down = float(lows[i]) - float(lows[window].min())

        # ── Bullish OB: a down candle followed by an up displacement ──
        if candle_down and move_up >= DISPLACEMENT_ATR * atr:
            broke = any(highs[window].max() > h for h in swing_highs) if swing_highs else True
            if broke:
                blocks.append(_build(
                    BULLISH, top=float(highs[i]), bottom=float(lows[i]), index=i,
                    displacement=move_up / atr,
                    volume_ratio=_vol_ratio(volumes, i),
                    future_low=float(lows[i + 1:].min()),
                    future_close_min=float(closes[i + 1:].min()),
                    price=price,
                ))

        # ── Bearish OB: an up candle followed by a down displacement ──
        elif candle_up and move_down >= DISPLACEMENT_ATR * atr:
            broke = any(lows[window].min() < l for l in swing_lows) if swing_lows else True
            if broke:
                blocks.append(_build(
                    BEARISH, top=float(highs[i]), bottom=float(lows[i]), index=i,
                    displacement=move_down / atr,
                    volume_ratio=_vol_ratio(volumes, i),
                    future_high=float(highs[i + 1:].max()),
                    future_close_max=float(closes[i + 1:].max()),
                    price=price,
                ))

    # Keep the strongest, most recent blocks.
    blocks.sort(key=lambda b: (b.index, b.strength), reverse=True)
    blocks = blocks[:max_blocks]

    fresh_or_mitigated = [b for b in blocks if b.state != INVALIDATED]
    nearest = min(
        fresh_or_mitigated,
        key=lambda b: b.distance_atr(price, atr),
        default=None,
    )

    if nearest is None:
        return OrderBlockState(
            blocks=blocks, reason='order blocks found but all invalidated',
        )

    distance = nearest.distance_atr(price, atr)
    # A block confirms most strongly when price is sitting inside or right at it.
    proximity = max(0.0, 1.0 - min(1.0, distance / 2.0))
    score = round(nearest.strength * proximity, 4)

    return OrderBlockState(
        blocks=blocks,
        nearest=nearest,
        direction=nearest.direction,
        score=score,
        distance_atr=round(distance, 3),
        reason=(
            f'{nearest.state} {nearest.direction} order block '
            f'{distance:.1f} ATR from price'
        ),
    )


def _build(
    direction: str,
    top: float,
    bottom: float,
    index: int,
    displacement: float,
    volume_ratio: float,
    price: float,
    future_low: float = None,
    future_close_min: float = None,
    future_high: float = None,
    future_close_max: float = None,
) -> OrderBlock:
    """Assemble one block, classifying its lifecycle state and strength."""
    if direction == BULLISH:
        # Invalidated if a later candle CLOSED below the block; mitigated if a
        # wick tapped in; fresh otherwise.
        if future_close_min is not None and future_close_min < bottom:
            state = INVALIDATED
        elif future_low is not None and future_low <= top:
            state = MITIGATED
        else:
            state = FRESH
    else:
        if future_close_max is not None and future_close_max > top:
            state = INVALIDATED
        elif future_high is not None and future_high >= bottom:
            state = MITIGATED
        else:
            state = FRESH

    # Strength: displacement is the dominant signal; volume and freshness add.
    disp_score = min(1.0, displacement / 3.0)
    vol_score = min(1.0, volume_ratio / 2.0)
    fresh_bonus = {FRESH: 1.0, MITIGATED: 0.6, INVALIDATED: 0.2}[state]
    strength = round((0.55 * disp_score + 0.25 * vol_score + 0.20) * fresh_bonus, 4)

    return OrderBlock(
        direction=direction, top=top, bottom=bottom, index=index, state=state,
        displacement_atr=round(displacement, 3), volume_ratio=round(volume_ratio, 3),
        strength=strength,
    )


def _vol_ratio(volumes, index: int, lookback: int = 20) -> float:
    """Volume of the block candle versus its trailing average."""
    start = max(0, index - lookback)
    window = volumes[start:index]
    avg = float(window.mean()) if len(window) else 0.0
    return (float(volumes[index]) / avg) if avg > 0 else 1.0
