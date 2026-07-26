"""Breakout Engine (spec §1, §3, §4).

Squeeze-release / structure break: after a volatility contraction, enter when
price closes beyond the recent range with participation. The stop anchor is the
broken level (range edge); a retest entry would be preferred in production to cut
fees (spec §5), but the signal here is the confirmed close-through.

Emits a V4Signal only when the regime router armed the 'breakout' engine (a
squeeze release) and the confluence count clears the gate. Pure, network-free.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from signals.indicators import compute_atr
from v4.engines.base import V4Signal, score_confluence
from v4.params import V4Params
from v4.regime import RegimeState


class BreakoutEngine:
    def __init__(self, params: Optional[V4Params] = None) -> None:
        self.params = params or V4Params()

    def generate(self, symbol: str, df: pd.DataFrame, regime: RegimeState) -> Optional[V4Signal]:
        p = self.params
        if regime is None or not regime.tradeable or regime.armed_engine != 'breakout':
            return None
        need = max(p.breakout_lookback, p.volume_lookback, p.adx_period) + 2
        if df is None or len(df) < need:
            return None

        atr = compute_atr(df['high'], df['low'], df['close'], period=p.adx_period).dropna()
        if atr.empty:
            return None

        last = df.iloc[-1]
        close = float(last['close'])
        atr_last = float(atr.iloc[-1])
        if atr_last <= 0:
            return None

        range_high = float(df['high'].iloc[-(p.breakout_lookback + 1):-1].max())
        range_low = float(df['low'].iloc[-(p.breakout_lookback + 1):-1].min())
        vol_avg = float(df['volume'].iloc[-(p.volume_lookback + 1):-1].mean())
        vol_last = float(last['volume'])

        # Direction: prefer the regime's allowed side; otherwise infer from the break.
        broke_up = close > range_high
        broke_down = close < range_low
        side = regime.allowed_side
        if side is None:
            side = 'long' if broke_up else ('short' if broke_down else None)
        if side not in ('long', 'short'):
            return None

        if side == 'long':
            trigger = broke_up
            structural_level = range_high
        else:
            trigger = broke_down
            structural_level = range_low
        if not trigger:
            return None

        flags = {
            'regime_alignment': True,
            'htf_trend': regime.allowed_side is None or regime.allowed_side == side,
            'structure': True,   # a defined range edge was broken
            'momentum': (broke_up and side == 'long') or (broke_down and side == 'short'),
            'volume': vol_avg > 0 and vol_last >= vol_avg,
            'volatility_setup': regime.breakout_candidate,  # squeeze release
        }
        count, parts = score_confluence(flags)
        if count < p.min_confluence:
            return None

        return V4Signal(
            symbol=symbol, side=side, engine='breakout', entry=close, atr=atr_last,
            structural_level=structural_level, confluence=count, confluence_parts=parts,
            reason=(
                f'breakout ({side}) close={close:.6f} '
                f'range=[{range_low:.6f},{range_high:.6f}] confluence={count}/6'
            ),
            regime_reason=regime.reason,
        )
