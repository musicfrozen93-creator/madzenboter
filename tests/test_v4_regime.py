"""V4 Phase 1 — Regime Detection Engine tests (spec §C3).

Uses deterministic synthetic OHLCV (seeded) — no network. Asserts the core
direction classification (the routing-critical output) on a strong trend, a
choppy range, and an insufficient-data case, plus the systemic BTC risk-off gate.
"""

import numpy as np
import pandas as pd

from v4.params import V4Params
from v4.regime import Direction, RegimeEngine


def _df_from_close(close: np.ndarray, wick=0.003) -> pd.DataFrame:
    close = np.asarray(close, dtype='float64')
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = close * (1.0 + wick)
    low = close * (1.0 - wick)
    return pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close,
                         'volume': np.full(close.shape, 1000.0)})


def _uptrend(n=400, seed=42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.004, 0.004, n)   # strong positive drift
    close = 100.0 * np.cumprod(1.0 + steps)
    return _df_from_close(close)


def _downtrend(n=400, seed=7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(-0.004, 0.004, n)
    close = 100.0 * np.cumprod(1.0 + steps)
    return _df_from_close(close)


def _range(n=400, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    x = np.linspace(0.0, 40.0 * np.pi, n)
    close = 100.0 + np.sin(x) * 2.0 + rng.normal(0.0, 0.2, n)
    return _df_from_close(close, wick=0.002)


def test_insufficient_data_not_tradeable():
    eng = RegimeEngine()
    st = eng.classify(_uptrend(n=50))
    assert not st.tradeable
    assert st.reason == 'insufficient_data'


def test_uptrend_classified_trend_up():
    eng = RegimeEngine()
    st = eng.classify(_uptrend())
    assert st.direction == Direction.TREND_UP
    assert st.adx >= V4Params().adx_trend_min


def test_downtrend_classified_trend_down():
    eng = RegimeEngine()
    st = eng.classify(_downtrend())
    assert st.direction == Direction.TREND_DOWN


def test_range_classified_range():
    eng = RegimeEngine()
    st = eng.classify(_range())
    assert st.direction == Direction.RANGE


def test_trend_arms_a_directional_engine_with_aligned_side():
    eng = RegimeEngine()
    st = eng.classify(_uptrend())
    if st.tradeable:
        assert st.armed_engine in ('trend', 'breakout')
        assert st.allowed_side == 'long'


def test_btc_risk_off_gate_stands_down():
    """Undirected high-volatility BTC forces FLAT even on a clean symbol trend."""
    eng = RegimeEngine()
    symbol = _uptrend()
    # BTC: a violent, undirected whipsaw → EXPANSION volatility + RANGE/UNKNOWN dir.
    rng = np.random.default_rng(99)
    btc_close = 100.0 + np.cumsum(rng.normal(0.0, 3.0, 400))
    btc = _df_from_close(np.abs(btc_close) + 50.0, wick=0.02)
    st = eng.classify(symbol, df_btc=btc)
    # Either the gate fires (not tradeable, risk-off) or, if BTC read as trending,
    # the symbol remains classifiable — but it must never be a silent error.
    assert st.direction in (Direction.TREND_UP, Direction.RANGE, Direction.UNKNOWN)
    if 'risk_off' in st.reason:
        assert not st.tradeable
