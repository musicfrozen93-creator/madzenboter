"""Trend Following Engine (spec §1, §3, §4).

Pullback-continuation: inside an established (regime-confirmed) trend, enter when
price pulls back to the fast reference MA and resumes in the trend direction. The
stop anchor is the most recent swing low (long) / swing high (short).

Emits a V4Signal only when the regime router has armed the 'trend' engine and the
confluence count clears the gate. Pure and network-free.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from signals.indicators import compute_atr, compute_ema
from v4.engines.base import V4Signal, score_confluence
from v4.indicators import compute_adx
from v4.params import V4Params
from v4.regime import RegimeState


class TrendEngine:
    def __init__(self, params: Optional[V4Params] = None) -> None:
        self.params = params or V4Params()

    def generate(self, symbol: str, df: pd.DataFrame, regime: RegimeState) -> Optional[V4Signal]:
        p = self.params
        if regime is None or not regime.tradeable or regime.armed_engine != 'trend':
            return None
        side = regime.allowed_side
        if side not in ('long', 'short'):
            return None
        need = max(p.trend_pullback_ema, p.swing_lookback, p.volume_lookback, p.adx_period) + 2
        if df is None or len(df) < need:
            return None

        ema = compute_ema(df['close'], period=p.trend_pullback_ema).dropna()
        atr = compute_atr(df['high'], df['low'], df['close'], period=p.adx_period).dropna()
        _, plus_di, minus_di = compute_adx(df['high'], df['low'], df['close'], period=p.adx_period)
        if ema.empty or atr.empty:
            return None

        last = df.iloc[-1]
        close = float(last['close'])
        open_ = float(last['open'])
        ema_last = float(ema.iloc[-1])
        atr_last = float(atr.iloc[-1])
        pdi = float(plus_di.iloc[-1]) if not plus_di.dropna().empty else 0.0
        mdi = float(minus_di.iloc[-1]) if not minus_di.dropna().empty else 0.0
        if atr_last <= 0 or ema_last <= 0:
            return None

        recent_lows = df['low'].iloc[-3:]
        recent_highs = df['high'].iloc[-3:]
        vol_avg = float(df['volume'].iloc[-(p.volume_lookback + 1):-1].mean())
        vol_last = float(last['volume'])

        swing_low = float(df['low'].iloc[-(p.swing_lookback + 1):-1].min())
        swing_high = float(df['high'].iloc[-(p.swing_lookback + 1):-1].max())

        if side == 'long':
            pulled_back = float(recent_lows.min()) <= ema_last
            resumed = close > ema_last and close > open_
            trigger = pulled_back and resumed
            structural_level = swing_low
            momentum_ok = pdi > mdi
        else:
            pulled_back = float(recent_highs.max()) >= ema_last
            resumed = close < ema_last and close < open_
            trigger = pulled_back and resumed
            structural_level = swing_high
            momentum_ok = mdi > pdi

        if not trigger:
            return None

        flags = {
            'regime_alignment': True,
            'htf_trend': True,   # regime already confirmed the trend direction
            'structure': pulled_back,
            'momentum': momentum_ok,
            'volume': vol_avg > 0 and vol_last >= vol_avg,
            'volatility_setup': True,   # regime armed trend under expansion/normal vol
        }
        count, parts = score_confluence(flags)
        if count < p.min_confluence:
            return None

        return V4Signal(
            symbol=symbol, side=side, engine='trend', entry=close, atr=atr_last,
            structural_level=structural_level, confluence=count, confluence_parts=parts,
            reason=(
                f'trend pullback-continuation ({side}) close={close:.6f} '
                f'ema{p.trend_pullback_ema}={ema_last:.6f} confluence={count}/6'
            ),
            regime_reason=regime.reason,
        )
