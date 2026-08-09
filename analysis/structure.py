"""Market Structure — swing detection, trend classification, BOS / CHoCH.

Market structure is read the way a discretionary trader reads it: find the
confirmed swing points, then ask whether the sequence is making higher highs and
higher lows (bullish), lower highs and lower lows (bearish), or neither (range).
A break of the most recent opposing swing is a Break Of Structure (BOS) when it
continues the prevailing trend, and a Change Of Character (CHoCH) when it breaks
against it.

Pure and network-free: every function takes an already-fetched OHLCV frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

# A swing needs this many bars on each side to be confirmed. The right-hand bars
# are what make a swing "confirmed" — a pivot is never called on the live bar.
DEFAULT_LEFT = 2
DEFAULT_RIGHT = 2

# How many recent swings the trend read considers.
TREND_SWING_WINDOW = 6

BULLISH = 'bullish'
BEARISH = 'bearish'
RANGE = 'range'

BOS = 'bos'
CHOCH = 'choch'


@dataclass(frozen=True)
class Swing:
    """One confirmed pivot point."""

    index: int
    price: float
    kind: str          # 'high' | 'low'

    @property
    def is_high(self) -> bool:
        return self.kind == 'high'


@dataclass
class StructureState:
    """The market-structure read for one timeframe."""

    trend: str = RANGE                      # 'bullish' | 'bearish' | 'range'
    event: Optional[str] = None             # 'bos' | 'choch' | None
    event_direction: Optional[str] = None   # 'bullish' | 'bearish' | None
    event_strength: float = 0.0             # break distance in ATR, capped at 3
    last_swing_high: Optional[float] = None
    last_swing_low: Optional[float] = None
    protective_low: Optional[float] = None  # swing to hide a long stop behind
    protective_high: Optional[float] = None # swing to hide a short stop behind
    higher_highs: bool = False
    higher_lows: bool = False
    lower_highs: bool = False
    lower_lows: bool = False
    swings: List[Swing] = field(default_factory=list)
    strength: float = 0.0                   # 0–1 confidence in the trend read
    reason: str = 'insufficient_data'

    @property
    def has_structure(self) -> bool:
        """True when enough swings were confirmed to read structure at all."""
        return len(self.swings) >= 4

    def supports(self, direction: str) -> bool:
        """True when structure agrees with a proposed trade direction."""
        return self.trend == (BULLISH if direction == 'long' else BEARISH)


def find_swings(
    df: pd.DataFrame, left: int = DEFAULT_LEFT, right: int = DEFAULT_RIGHT
) -> List[Swing]:
    """Find confirmed swing highs and lows, returned oldest first.

    A bar is a swing high when its high is the maximum of the window spanning
    `left` bars before and `right` bars after it (and likewise, inverted, for
    lows). The final `right` bars can never form a swing — that is deliberate,
    since an unconfirmed pivot is not a pivot.

    Consecutive same-kind swings are collapsed to the more extreme one so the
    result always alternates high/low, which is what the trend read and the
    Elliott labeller both require.
    """
    if df is None or len(df) < left + right + 1:
        return []

    highs = df['high'].to_numpy(dtype=float)
    lows = df['low'].to_numpy(dtype=float)
    n = len(df)

    raw: List[Swing] = []
    for i in range(left, n - right):
        window = slice(i - left, i + right + 1)
        if highs[i] >= highs[window].max():
            raw.append(Swing(index=i, price=float(highs[i]), kind='high'))
        # A single bar can be both (an inside-bar cluster); keep both candidates
        # and let the alternation pass below decide which survives.
        if lows[i] <= lows[window].min():
            raw.append(Swing(index=i, price=float(lows[i]), kind='low'))

    return _alternate(raw)


def _alternate(swings: List[Swing]) -> List[Swing]:
    """Collapse consecutive same-kind swings, keeping the more extreme one."""
    cleaned: List[Swing] = []
    for swing in swings:
        if not cleaned:
            cleaned.append(swing)
            continue
        previous = cleaned[-1]
        if previous.kind != swing.kind:
            cleaned.append(swing)
            continue
        # Same kind twice in a row: the more extreme pivot is the real one.
        better = (
            swing.price > previous.price if swing.is_high else swing.price < previous.price
        )
        if better:
            cleaned[-1] = swing
    return cleaned


def analyze_structure(
    df: pd.DataFrame,
    atr: float,
    left: int = DEFAULT_LEFT,
    right: int = DEFAULT_RIGHT,
) -> StructureState:
    """Read market structure: trend, the latest BOS/CHoCH, and protective swings.

    Args:
        df: OHLCV candles, oldest first.
        atr: Current ATR, used to express break strength in volatility units.
        left: Bars required to the left of a pivot.
        right: Bars required to the right of a pivot (confirmation).

    Returns:
        A StructureState. When fewer than four swings are confirmed the trend is
        reported as RANGE with reason 'insufficient_data' — never guessed.
    """
    swings = find_swings(df, left, right)
    if len(swings) < 4:
        return StructureState(swings=swings)

    recent = swings[-TREND_SWING_WINDOW:]
    highs = [s for s in recent if s.is_high]
    lows = [s for s in recent if not s.is_high]
    if len(highs) < 2 or len(lows) < 2:
        return StructureState(
            swings=swings,
            last_swing_high=highs[-1].price if highs else None,
            last_swing_low=lows[-1].price if lows else None,
            reason='insufficient_swings',
        )

    higher_highs = highs[-1].price > highs[-2].price
    lower_highs = highs[-1].price < highs[-2].price
    higher_lows = lows[-1].price > lows[-2].price
    lower_lows = lows[-1].price < lows[-2].price

    if higher_highs and higher_lows:
        trend, strength, reason = BULLISH, 1.0, 'higher highs and higher lows'
    elif lower_highs and lower_lows:
        trend, strength, reason = BEARISH, 1.0, 'lower highs and lower lows'
    elif higher_highs or higher_lows:
        trend, strength, reason = BULLISH, 0.5, 'partially bullish structure'
    elif lower_highs or lower_lows:
        trend, strength, reason = BEARISH, 0.5, 'partially bearish structure'
    else:
        trend, strength, reason = RANGE, 0.0, 'no directional structure'

    # ── Break of the most recent confirmed swing ──
    close = float(df['close'].iloc[-1])
    event: Optional[str] = None
    event_direction: Optional[str] = None
    event_strength = 0.0

    last_high = highs[-1]
    last_low = lows[-1]

    if close > last_high.price:
        event_direction = BULLISH
        event_strength = _break_strength(close - last_high.price, atr)
    elif close < last_low.price:
        event_direction = BEARISH
        event_strength = _break_strength(last_low.price - close, atr)

    if event_direction is not None:
        # Continuing the prevailing trend is a BOS; breaking against it is a
        # change of character — the earliest structural warning of a reversal.
        opposing = (
            (event_direction == BULLISH and trend == BEARISH)
            or (event_direction == BEARISH and trend == BULLISH)
        )
        event = CHOCH if opposing else BOS
        reason = f'{event_direction} {event.upper()} — {reason}'

    return StructureState(
        trend=trend,
        event=event,
        event_direction=event_direction,
        event_strength=event_strength,
        last_swing_high=last_high.price,
        last_swing_low=last_low.price,
        protective_low=_protective(lows, close, below=True),
        protective_high=_protective(highs, close, below=False),
        higher_highs=higher_highs,
        higher_lows=higher_lows,
        lower_highs=lower_highs,
        lower_lows=lower_lows,
        swings=swings,
        strength=strength,
        reason=reason,
    )


def _break_strength(distance: float, atr: float) -> float:
    """Express a break distance in ATR units, capped at 3 (beyond that is noise)."""
    if atr <= 0:
        return 0.0
    return min(3.0, distance / atr)


def _protective(swings: List[Swing], close: float, below: bool) -> Optional[float]:
    """The nearest swing on the protective side of price, for stop placement.

    For a long, that is the highest swing low still BELOW price — the level that,
    if lost, invalidates the structure the trade is based on.
    """
    if below:
        candidates = [s.price for s in swings if s.price < close]
        return max(candidates) if candidates else None
    candidates = [s.price for s in swings if s.price > close]
    return min(candidates) if candidates else None
