"""VWAP Engine.

Volume-Weighted Average Price anchored to the start of the analysed window — the
average price every unit of volume actually traded at. Institutions benchmark
fills against it, so price relative to VWAP is a widely-watched bias:

  • above VWAP  → buyers in control on the session
  • below VWAP  → sellers in control

VWAP trend is the slope of the VWAP line over the recent window.

Confirmation layer only. Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from analysis.structure import BEARISH, BULLISH

# Bars over which the VWAP slope (its trend) is measured.
SLOPE_LOOKBACK = 10

# Price within this fraction of ATR of VWAP is "at" it — no directional read.
AT_VWAP_ATR = 0.25


@dataclass
class VWAPState:
    """The VWAP read for one timeframe."""

    vwap: float = 0.0
    price: float = 0.0
    distance: float = 0.0           # price − vwap
    distance_atr: float = 0.0
    above: bool = False
    below: bool = False
    trend: str = 'flat'             # 'rising' | 'falling' | 'flat'
    direction: str = 'neutral'
    score: float = 0.0
    reason: str = 'vwap_unavailable'


def compute_vwap(df: pd.DataFrame, atr: float) -> VWAPState:
    """Compute the anchored VWAP and price's relationship to it.

    Args:
        df: OHLCV candles, oldest first.
        atr: Current ATR, the scale for the distance and the "at VWAP" band.

    Returns:
        A VWAPState. `direction` combines which side of VWAP price is on with the
        VWAP's own slope — both must agree for a full-strength read.
    """
    if df is None or len(df) < SLOPE_LOOKBACK + 1:
        return VWAPState()

    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    volume = df['volume'].to_numpy(dtype=float)

    typical = (high + low + close) / 3.0
    cum_vol = np.cumsum(volume)
    cum_pv = np.cumsum(typical * volume)
    # Guard against zero-volume prefixes.
    with np.errstate(divide='ignore', invalid='ignore'):
        vwap_series = np.where(cum_vol > 0, cum_pv / cum_vol, typical)

    vwap = float(vwap_series[-1])
    price = float(close[-1])
    if vwap <= 0 or atr <= 0:
        return VWAPState(vwap=vwap, price=price)

    distance = price - vwap
    distance_atr = distance / atr

    # Slope of VWAP over the recent window → its trend.
    past = float(vwap_series[-(SLOPE_LOOKBACK + 1)])
    slope = (vwap - past) / atr if atr > 0 else 0.0
    if slope > 0.1:
        trend = 'rising'
    elif slope < -0.1:
        trend = 'falling'
    else:
        trend = 'flat'

    above = distance_atr > AT_VWAP_ATR
    below = distance_atr < -AT_VWAP_ATR

    if not above and not below:
        return VWAPState(
            vwap=round(vwap, 10), price=price, distance=round(distance, 10),
            distance_atr=round(distance_atr, 3), above=False, below=False,
            trend=trend, direction='neutral', score=0.15,
            reason=f'price at VWAP ({distance_atr:+.2f} ATR), no bias',
        )

    direction = BULLISH if above else BEARISH
    aligned = (trend == 'rising' and above) or (trend == 'falling' and below)
    # Distance saturates: being far above VWAP confirms bias but risks reversion.
    dist_score = min(1.0, abs(distance_atr) / 2.0)
    score = round((0.6 * dist_score + (0.4 if aligned else 0.1)), 4)

    return VWAPState(
        vwap=round(vwap, 10), price=price, distance=round(distance, 10),
        distance_atr=round(distance_atr, 3), above=above, below=below,
        trend=trend, direction=direction, score=score,
        reason=(
            f'price {"above" if above else "below"} a {trend} VWAP '
            f'({distance_atr:+.2f} ATR)'
        ),
    )
