"""MACD Engine.

Moving Average Convergence Divergence: the 12/26 EMA spread, its 9-period signal
line, and the histogram between them. A bullish cross (MACD rising through the
signal) and a positive, expanding histogram confirm upward momentum; the mirror
confirms downward.

Reuses `signals.indicators.compute_ema` — no EMA is reimplemented here.

Confirmation layer only. Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from analysis.structure import BEARISH, BULLISH
from signals.indicators import compute_ema

FAST = 12
SLOW = 26
SIGNAL = 9

# A cross counts as "recent" within this many bars.
CROSS_RECENCY = 3


@dataclass
class MACDState:
    """The MACD read for one timeframe."""

    macd: float = 0.0
    signal: float = 0.0
    histogram: float = 0.0
    bullish_cross: bool = False
    bearish_cross: bool = False
    momentum: str = 'neutral'       # 'bullish' | 'bearish' | 'neutral'
    direction: str = 'neutral'
    score: float = 0.0
    reason: str = 'macd_unavailable'


def compute_macd(df: pd.DataFrame) -> MACDState:
    """Compute MACD, its signal, histogram, and the latest cross.

    Returns:
        A MACDState whose `direction` reflects histogram sign and momentum, with
        a stronger score when a fresh cross confirms it.
    """
    if df is None or len(df) < SLOW + SIGNAL + 2:
        return MACDState()

    close = df['close']
    ema_fast = compute_ema(close, period=FAST)
    ema_slow = compute_ema(close, period=SLOW)
    macd_line = (ema_fast - ema_slow).dropna()
    if len(macd_line) < SIGNAL + 2:
        return MACDState()

    signal_line = compute_ema(macd_line, period=SIGNAL).dropna()
    if len(signal_line) < 2:
        return MACDState()

    macd_line = macd_line.loc[signal_line.index]
    hist = macd_line - signal_line

    macd_now = float(macd_line.iloc[-1])
    signal_now = float(signal_line.iloc[-1])
    hist_now = float(hist.iloc[-1])

    # Detect the most recent cross within the recency window.
    bullish_cross = bearish_cross = False
    recent = hist.iloc[-(CROSS_RECENCY + 1):]
    values = recent.to_numpy()
    for i in range(1, len(values)):
        if values[i - 1] <= 0 < values[i]:
            bullish_cross = True
        elif values[i - 1] >= 0 > values[i]:
            bearish_cross = True

    # Momentum from histogram sign and whether it is expanding.
    prev_hist = float(hist.iloc[-2])
    expanding = abs(hist_now) > abs(prev_hist)
    if hist_now > 0:
        momentum = 'bullish'
        direction = BULLISH
    elif hist_now < 0:
        momentum = 'bearish'
        direction = BEARISH
    else:
        momentum = 'neutral'
        direction = 'neutral'

    # Score: a fresh cross is the strongest confirmation; otherwise histogram
    # sign + expansion.
    if (direction == BULLISH and bullish_cross) or (direction == BEARISH and bearish_cross):
        score = 0.95
    elif direction != 'neutral':
        score = 0.65 if expanding else 0.45
    else:
        score = 0.1

    reason = _reason(direction, bullish_cross, bearish_cross, hist_now, expanding)

    return MACDState(
        macd=round(macd_now, 10), signal=round(signal_now, 10),
        histogram=round(hist_now, 10),
        bullish_cross=bullish_cross, bearish_cross=bearish_cross,
        momentum=momentum, direction=direction, score=round(score, 4),
        reason=reason,
    )


def _reason(direction, bull_cross, bear_cross, hist, expanding) -> str:
    if bull_cross:
        return 'bullish MACD cross'
    if bear_cross:
        return 'bearish MACD cross'
    if direction == BULLISH:
        return f'MACD histogram positive and {"expanding" if expanding else "fading"}'
    if direction == BEARISH:
        return f'MACD histogram negative and {"expanding" if expanding else "fading"}'
    return 'MACD flat'
