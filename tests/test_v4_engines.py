"""V4 Phase 2 — Trend / Breakout engine tests (spec §3, §4)."""

import numpy as np
import pandas as pd

from v4.engines import BreakoutEngine, TrendEngine
from v4.params import V4Params
from v4.regime import Direction, RegimeEngine, RegimeState, Volatility


def _df(close, high=None, low=None, open_=None, volume=None):
    close = np.asarray(close, dtype='float64')
    n = len(close)
    open_ = np.asarray(open_, dtype='float64') if open_ is not None else np.concatenate([[close[0]], close[:-1]])
    high = np.asarray(high, dtype='float64') if high is not None else close * 1.002
    low = np.asarray(low, dtype='float64') if low is not None else close * 0.998
    volume = np.asarray(volume, dtype='float64') if volume is not None else np.full(n, 1000.0)
    return pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume})


def _armed_trend(side='long'):
    return RegimeState(
        direction=Direction.TREND_UP if side == 'long' else Direction.TREND_DOWN,
        volatility=Volatility.NORMAL, adx=30.0, tradeable=True,
        armed_engine='trend', allowed_side=side, breakout_candidate=False,
        reason='trend_armed',
    )


def _armed_breakout(side='long'):
    return RegimeState(
        direction=Direction.TREND_UP if side == 'long' else Direction.TREND_DOWN,
        volatility=Volatility.EXPANSION, adx=28.0, tradeable=True,
        armed_engine='breakout', allowed_side=side, breakout_candidate=True,
        reason='breakout_armed',
    )


def test_trend_engine_no_signal_when_not_armed():
    eng = TrendEngine()
    flat = RegimeState(Direction.RANGE, Volatility.CONTRACTION, 15.0, False, None, None, False, 'flat')
    df = _df(np.linspace(100, 120, 120))
    assert eng.generate('X/USDT:USDT', df, flat) is None


def test_trend_engine_fires_on_pullback_continuation_long():
    p = V4Params()
    eng = TrendEngine(p)
    # Uptrend base, a dip to the EMA, then a strong bullish close above it.
    base = list(np.linspace(100, 130, 120))
    # Force the last 3 bars into a pullback-then-resume with a big up close + volume.
    base[-3] = 128.0
    base[-2] = 124.0   # pullback (dips toward/below EMA20)
    base[-1] = 131.0   # resume: close well above EMA and above open
    close = np.array(base)
    open_ = np.concatenate([[close[0]], close[:-1]])
    low = close * 0.995
    low[-2] = 118.0    # the pullback bar tags below the EMA
    high = close * 1.005
    volume = np.full(len(close), 1000.0)
    volume[-1] = 5000.0  # participation
    df = _df(close, high=high, low=low, open_=open_, volume=volume)
    sig = eng.generate('X/USDT:USDT', df, _armed_trend('long'))
    assert sig is not None, 'expected a trend signal on pullback-continuation'
    assert sig.side == 'long' and sig.engine == 'trend'
    assert sig.confluence >= p.min_confluence
    assert sig.structural_level < sig.entry   # stop anchor (swing low) below entry


def test_breakout_engine_fires_on_range_break_long():
    p = V4Params()
    eng = BreakoutEngine(p)
    # A tight range then a decisive break above it on volume.
    rng = 100.0 + np.sin(np.linspace(0, 8 * np.pi, 60)) * 0.5
    close = np.concatenate([rng, [102.5]])   # last bar breaks above the ~100.5 high
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = close * 1.001
    high[:-1] = np.maximum(high[:-1], 100.6)
    low = close * 0.999
    volume = np.full(len(close), 1000.0)
    volume[-1] = 6000.0
    df = _df(close, high=high, low=low, open_=open_, volume=volume)
    sig = eng.generate('X/USDT:USDT', df, _armed_breakout('long'))
    assert sig is not None, 'expected a breakout signal on range break'
    assert sig.side == 'long' and sig.engine == 'breakout'
    assert sig.confluence >= p.min_confluence
    assert sig.structural_level <= sig.entry


def test_breakout_engine_silent_without_break():
    eng = BreakoutEngine()
    rng = 100.0 + np.sin(np.linspace(0, 8 * np.pi, 80)) * 0.5
    df = _df(rng)   # no break on the last bar
    assert eng.generate('X/USDT:USDT', df, _armed_breakout('long')) is None
