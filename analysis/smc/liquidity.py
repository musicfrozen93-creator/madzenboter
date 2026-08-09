"""Liquidity Engine (Smart Money Concepts).

Liquidity sits where clusters of stop orders rest: just above equal highs
(buy-side liquidity, BSL) and just below equal lows (sell-side liquidity, SSL).
Smart money drives price into those pools to fill size, then reverses — a
liquidity sweep (or grab).

  • Equal highs / lows  swing points at the same price (a resting pool)
  • Liquidity sweep      price wicks BEYOND a pool then closes back inside
  • Liquidity grab       the same, on the most recent candle (actionable now)

The directional read is contrarian to the swept side: sweeping SSL (lows) is
bullish; sweeping BSL (highs) is bearish.

Confirmation layer only. Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from analysis.structure import BEARISH, BULLISH, Swing

BUY_SIDE = 'buy_side'      # liquidity above equal highs
SELL_SIDE = 'sell_side'    # liquidity below equal lows

# Swings within this fraction of ATR of each other count as "equal".
EQUAL_TOLERANCE_ATR = 0.35

# A sweep must poke at least this far beyond the pool, in ATR.
SWEEP_MIN_ATR = 0.05

# A grab is a sweep on one of the last N candles.
GRAB_RECENCY = 3


@dataclass(frozen=True)
class LiquidityPool:
    """A cluster of equal highs or lows where stops rest."""

    side: str                 # 'buy_side' | 'sell_side'
    price: float
    touches: int
    swept: bool               # price has poked beyond it and closed back
    grabbed: bool             # the sweep happened on a very recent candle


@dataclass
class LiquidityState:
    """The liquidity read for one timeframe."""

    pools: List[LiquidityPool] = field(default_factory=list)
    last_sweep: Optional[LiquidityPool] = None
    direction: str = 'neutral'          # contrarian to the swept side
    score: float = 0.0
    reason: str = 'no_liquidity_events'

    @property
    def buy_side_pools(self) -> List[LiquidityPool]:
        return [p for p in self.pools if p.side == BUY_SIDE]

    @property
    def sell_side_pools(self) -> List[LiquidityPool]:
        return [p for p in self.pools if p.side == SELL_SIDE]

    @property
    def equal_highs(self) -> int:
        return sum(1 for p in self.buy_side_pools if p.touches >= 2)

    @property
    def equal_lows(self) -> int:
        return sum(1 for p in self.sell_side_pools if p.touches >= 2)


def detect_liquidity(
    df: pd.DataFrame, swings: List[Swing], atr: float
) -> LiquidityState:
    """Detect liquidity pools and the most recent sweep.

    Args:
        df: OHLCV candles, oldest first.
        swings: Confirmed swings — the raw material for equal highs/lows.
        atr: Current ATR, sets the equality tolerance and sweep threshold.

    Returns:
        A LiquidityState whose `direction` is contrarian to the last swept pool.
    """
    if df is None or len(df) < 3 or atr <= 0 or not swings:
        return LiquidityState()

    highs = df['high'].to_numpy(dtype=float)
    lows = df['low'].to_numpy(dtype=float)
    closes = df['close'].to_numpy(dtype=float)
    n = len(df)
    tolerance = EQUAL_TOLERANCE_ATR * atr

    swing_highs = [s for s in swings if s.is_high]
    swing_lows = [s for s in swings if not s.is_high]

    pools: List[LiquidityPool] = []
    pools.extend(_pools(swing_highs, BUY_SIDE, tolerance, highs, lows, closes, atr, n))
    pools.extend(_pools(swing_lows, SELL_SIDE, tolerance, highs, lows, closes, atr, n))

    swept = [p for p in pools if p.swept]
    # The most relevant sweep is the most recently grabbed, else any sweep of the
    # strongest pool.
    grabbed = [p for p in swept if p.grabbed]
    last_sweep = None
    if grabbed:
        last_sweep = max(grabbed, key=lambda p: p.touches)
    elif swept:
        last_sweep = max(swept, key=lambda p: p.touches)

    if last_sweep is None:
        return LiquidityState(
            pools=pools,
            reason=(
                f'{len([p for p in pools if p.touches >= 2])} liquidity pools, '
                'none swept'
            ) if pools else 'no_liquidity_events',
        )

    # Contrarian: sweeping sell-side (lows) is bullish, buy-side (highs) bearish.
    direction = BULLISH if last_sweep.side == SELL_SIDE else BEARISH
    base = 0.75 if last_sweep.grabbed else 0.45
    depth_bonus = min(0.25, 0.08 * last_sweep.touches)
    score = round(min(1.0, base + depth_bonus), 4)

    return LiquidityState(
        pools=pools,
        last_sweep=last_sweep,
        direction=direction,
        score=score,
        reason=(
            f'{"grab" if last_sweep.grabbed else "sweep"} of '
            f'{last_sweep.side.replace("_", "-")} liquidity at {last_sweep.price:.8f} '
            f'→ {direction}'
        ),
    )


def _pools(
    swing_points: List[Swing],
    side: str,
    tolerance: float,
    highs,
    lows,
    closes,
    atr: float,
    n: int,
) -> List[LiquidityPool]:
    """Cluster equal swing highs/lows and test each cluster for a sweep."""
    if not swing_points:
        return []

    ordered = sorted(swing_points, key=lambda s: s.price)
    clusters: List[List[Swing]] = [[ordered[0]]]
    for swing in ordered[1:]:
        centre = sum(s.price for s in clusters[-1]) / len(clusters[-1])
        if abs(swing.price - centre) <= tolerance:
            clusters[-1].append(swing)
        else:
            clusters.append([swing])

    pools: List[LiquidityPool] = []
    for members in clusters:
        level = sum(s.price for s in members) / len(members)
        # The pool forms once its last swing is confirmed.
        formed_at = max(s.index for s in members)
        swept, grabbed = _sweep_after(
            side, level, formed_at, highs, lows, closes, atr, n
        )
        pools.append(LiquidityPool(
            side=side, price=level, touches=len(members),
            swept=swept, grabbed=grabbed,
        ))
    return pools


def _sweep_after(
    side: str, level: float, formed_at: int, highs, lows, closes, atr: float, n: int
) -> tuple[bool, bool]:
    """Did price poke beyond `level` after it formed, then close back inside?"""
    threshold = SWEEP_MIN_ATR * atr
    for i in range(formed_at + 1, n):
        if side == BUY_SIDE:
            poked = highs[i] > level + threshold
            closed_back = closes[i] < level
        else:
            poked = lows[i] < level - threshold
            closed_back = closes[i] > level
        if poked and closed_back:
            grabbed = i >= n - GRAB_RECENCY
            return True, grabbed
    return False, False
