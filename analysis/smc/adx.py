"""ADX Engine.

Average Directional Index: trend STRENGTH (not direction), with the +DI / −DI
pair giving direction. ADX answers "is there a trend worth trading?" — high ADX
confirms whichever direction the other modules found; low ADX warns the market
is ranging and directional setups are less reliable.

Reuses `v4.indicators.compute_adx` — the Wilder ADX/DI already implemented in
Phase 0. Nothing is recomputed here.

Confirmation layer only. Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from analysis.structure import BEARISH, BULLISH
from v4.indicators import compute_adx

PERIOD = 14

# Wells Wilder's conventional thresholds.
STRONG_TREND = 25.0
NO_TREND = 20.0


@dataclass
class ADXState:
    """The ADX read for one timeframe."""

    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    trend_strength: str = 'no_trend'    # 'strong' | 'weak' | 'no_trend'
    strong_trend: bool = False
    weak_trend: bool = False
    no_trend: bool = True
    direction: str = 'neutral'
    score: float = 0.0
    reason: str = 'adx_unavailable'


def compute_adx_state(df: pd.DataFrame) -> ADXState:
    """Compute ADX, DI+/DI−, and classify trend strength.

    Returns:
        An ADXState. `direction` comes from the DI pair; `score` scales with ADX
        so a strong trend confirms strongly and a flat market barely at all.
    """
    if df is None or len(df) < PERIOD * 2 + 2:
        return ADXState()

    adx_series, plus_di, minus_di = compute_adx(
        df['high'], df['low'], df['close'], period=PERIOD
    )
    adx_clean = adx_series.dropna()
    if adx_clean.empty:
        return ADXState()

    adx = float(adx_clean.iloc[-1])
    pdi = float(plus_di.dropna().iloc[-1]) if not plus_di.dropna().empty else 0.0
    mdi = float(minus_di.dropna().iloc[-1]) if not minus_di.dropna().empty else 0.0

    if adx >= STRONG_TREND:
        strength_label, strong, weak, no = 'strong', True, False, False
    elif adx >= NO_TREND:
        strength_label, strong, weak, no = 'weak', False, True, False
    else:
        strength_label, strong, weak, no = 'no_trend', False, False, True

    direction = 'neutral'
    if strong or weak:
        direction = BULLISH if pdi > mdi else BEARISH

    # Score scales linearly from the no-trend floor up to a strong reading.
    if adx <= NO_TREND:
        score = 0.1
    else:
        score = min(1.0, 0.4 + (adx - NO_TREND) / 40.0)

    return ADXState(
        adx=round(adx, 2), plus_di=round(pdi, 2), minus_di=round(mdi, 2),
        trend_strength=strength_label, strong_trend=strong, weak_trend=weak,
        no_trend=no, direction=direction, score=round(score, 4),
        reason=(
            f'ADX {adx:.1f} ({strength_label.replace("_", " ")}), '
            f'{"+DI>−DI" if pdi > mdi else "−DI>+DI"}'
        ),
    )
