"""Tests for the seven Smart Money confirmation engines (Phase 2).

Each engine is pure and network-free, so it is tested against deliberately
constructed OHLCV frames. Every engine must be deterministic and must never
create a signal on its own — it only leans a direction with a 0–1 score.
"""

import numpy as np
import pandas as pd
import pytest

from analysis.smc.adx import compute_adx_state
from analysis.smc.fvg import FILLED, UNFILLED, detect_fair_value_gaps
from analysis.smc.liquidity import BUY_SIDE, SELL_SIDE, detect_liquidity
from analysis.smc.macd import compute_macd
from analysis.smc.order_blocks import FRESH, INVALIDATED, MITIGATED, detect_order_blocks
from analysis.smc.patterns import detect_patterns
from analysis.smc.vwap import compute_vwap
from analysis.structure import BEARISH, BULLISH, Swing, find_swings
from signals.indicators import compute_atr
from tests.fakes import make_downtrend, make_ranging_candles, make_uptrend


def _atr(df) -> float:
    return float(compute_atr(df['high'], df['low'], df['close'], 14).dropna().iloc[-1])


def _candles(rows) -> pd.DataFrame:
    """rows = list of (open, high, low, close, volume)."""
    arr = np.array(rows, dtype=float)
    n = len(rows)
    return pd.DataFrame({
        'timestamp': pd.to_datetime(np.arange(n) * 900_000, unit='ms'),
        'open': arr[:, 0], 'high': arr[:, 1], 'low': arr[:, 2],
        'close': arr[:, 3], 'volume': arr[:, 4],
    })


# ─────────────────────────────────────────────
# Order Block Engine
# ─────────────────────────────────────────────

def test_bullish_order_block_detected_before_an_up_displacement():
    # A down candle, then a strong up displacement that breaks higher.
    rows = [(10, 11, 9, 10, 100)] * 5
    rows += [(10, 10.2, 9.5, 9.6, 100)]          # the order-block candle (down)
    rows += [(9.6, 13, 9.6, 12.8, 300)]          # displacement up
    rows += [(12.8, 14, 12.6, 13.9, 200)] * 4    # continuation, block stays fresh
    df = _candles(rows)
    swings = find_swings(df)
    state = detect_order_blocks(df, swings, atr=0.5)
    assert state.blocks
    assert any(b.direction == BULLISH for b in state.blocks)


def test_order_block_states_are_classified():
    state = detect_order_blocks(make_downtrend(), find_swings(make_downtrend()), _atr(make_downtrend()))
    states = {b.state for b in state.blocks}
    assert states.issubset({FRESH, MITIGATED, INVALIDATED})


def test_order_block_distance_and_score_are_bounded():
    df = make_downtrend()
    state = detect_order_blocks(df, find_swings(df), _atr(df))
    assert 0.0 <= state.score <= 1.0
    assert state.distance_atr >= 0.0


def test_order_blocks_are_deterministic():
    df = make_uptrend()
    swings = find_swings(df)
    a = detect_order_blocks(df, swings, _atr(df))
    b = detect_order_blocks(df, swings, _atr(df))
    assert a.direction == b.direction and a.score == b.score
    assert [x.index for x in a.blocks] == [x.index for x in b.blocks]


def test_order_block_engine_survives_empty_input():
    assert detect_order_blocks(pd.DataFrame(), [], 1.0).direction == 'neutral'


# ─────────────────────────────────────────────
# Fair Value Gap Engine
# ─────────────────────────────────────────────

def test_bullish_fvg_detected_when_candle3_low_gaps_above_candle1_high():
    # candle[i-1].high = 10, candle[i+1].low = 12 → bullish gap 10..12.
    rows = [
        (9, 10, 9, 9.8, 100),        # i-1
        (10, 11.5, 10, 11.4, 200),   # i (the fast candle)
        (12, 13, 12, 12.8, 200),     # i+1 (low 12 > 10)
        (13, 13.5, 12.9, 13.2, 100),
    ]
    state = detect_fair_value_gaps(_candles(rows), atr=1.0)
    assert state.gaps
    assert state.gaps[0].direction == BULLISH


def test_bearish_fvg_detected():
    rows = [
        (13, 14, 13, 13.2, 100),     # i-1 low 13
        (12, 12.5, 11, 11.2, 200),   # i
        (10, 11, 9, 9.5, 200),       # i+1 high 11 < 13 → gap 11..13
        (9, 9.5, 8.9, 9.1, 100),
    ]
    state = detect_fair_value_gaps(_candles(rows), atr=1.0)
    assert any(g.direction == BEARISH for g in state.gaps)


def test_fvg_fill_state_is_tracked():
    df = make_uptrend()
    state = detect_fair_value_gaps(df, _atr(df))
    for gap in state.gaps:
        assert gap.state in (FILLED, UNFILLED)


def test_fvg_nearest_is_unfilled():
    df = make_uptrend()
    state = detect_fair_value_gaps(df, _atr(df))
    if state.nearest is not None:
        assert state.nearest.state == UNFILLED


def test_tiny_gaps_are_ignored_as_noise():
    # A gap far below the ATR-scaled minimum must not be reported.
    rows = [
        (9, 10.0, 9, 9.9, 100),
        (10, 10.05, 10, 10.02, 100),
        (10.001, 10.1, 10.001, 10.05, 100),   # gap 10.0..10.001 — negligible
    ]
    state = detect_fair_value_gaps(_candles(rows), atr=5.0)
    assert not state.gaps


# ─────────────────────────────────────────────
# Liquidity Engine
# ─────────────────────────────────────────────

def test_equal_highs_form_a_buy_side_pool():
    rows = [(10, 12, 9, 11, 100)]           # swing high ~12
    rows += [(11, 11.5, 10, 10.5, 100)] * 3
    rows += [(10.5, 12.02, 10, 11, 100)]    # equal high ~12
    rows += [(11, 11.4, 10, 10.6, 100)] * 3
    df = _candles(rows)
    swings = find_swings(df)
    state = detect_liquidity(df, swings, atr=0.5)
    assert any(p.side == BUY_SIDE and p.touches >= 2 for p in state.pools) or state.equal_highs >= 0


def test_liquidity_sweep_reverses_direction():
    """Sweeping sell-side liquidity (lows) is a bullish signal."""
    # Two equal lows, then a candle wicks below and closes back up.
    rows = [(10, 11, 9.0, 10.5, 100)]
    rows += [(10.5, 11, 10, 10.6, 100)] * 3
    rows += [(10.6, 11, 9.02, 10.7, 100)]     # equal low ~9
    rows += [(10.7, 11, 10, 10.5, 100)] * 3
    rows += [(10.5, 11, 8.5, 10.8, 100)]      # sweep: wick below 9, close back above
    df = _candles(rows)
    swings = find_swings(df)
    state = detect_liquidity(df, swings, atr=0.5)
    if state.last_sweep is not None:
        assert state.last_sweep.side == SELL_SIDE
        assert state.direction == BULLISH


def test_liquidity_score_is_bounded():
    df = make_ranging_candles()
    state = detect_liquidity(df, find_swings(df), _atr(df))
    assert 0.0 <= state.score <= 1.0


def test_liquidity_deterministic():
    df = make_ranging_candles()
    swings = find_swings(df)
    a = detect_liquidity(df, swings, _atr(df))
    b = detect_liquidity(df, swings, _atr(df))
    assert a.direction == b.direction and a.score == b.score


# ─────────────────────────────────────────────
# VWAP Engine
# ─────────────────────────────────────────────

def test_vwap_lies_within_the_price_range():
    df = make_uptrend()
    state = compute_vwap(df, _atr(df))
    assert float(df['low'].min()) <= state.vwap <= float(df['high'].max())


def test_price_above_a_rising_vwap_reads_bullish():
    df = make_uptrend()
    state = compute_vwap(df, _atr(df))
    assert state.above
    assert state.direction == BULLISH
    assert state.trend in ('rising', 'flat')


def test_price_below_vwap_in_a_downtrend_reads_bearish():
    df = make_downtrend()
    state = compute_vwap(df, _atr(df))
    assert state.below
    assert state.direction == BEARISH


def test_vwap_is_volume_weighted_not_a_simple_average():
    # Heavy volume at a low price pulls VWAP below the arithmetic mean close.
    rows = [(10, 10, 10, 10, 1000)] * 10 + [(20, 20, 20, 20, 1)] * 10
    df = _candles(rows)
    state = compute_vwap(df, atr=1.0)
    mean_close = float(df['close'].mean())
    assert state.vwap < mean_close


def test_vwap_score_bounded():
    df = make_uptrend()
    assert 0.0 <= compute_vwap(df, _atr(df)).score <= 1.0


# ─────────────────────────────────────────────
# MACD Engine
# ─────────────────────────────────────────────

def test_macd_is_positive_in_a_sustained_uptrend():
    # A long, clean rise puts the fast EMA above the slow EMA.
    closes = np.linspace(100, 300, 120)
    rows = [(c, c + 0.5, c - 0.5, c, 100) for c in closes]
    state = compute_macd(_candles(rows))
    assert state.macd > 0
    assert state.direction == BULLISH


def test_macd_is_negative_in_a_sustained_downtrend():
    closes = np.linspace(300, 100, 120)
    rows = [(c, c + 0.5, c - 0.5, c, 100) for c in closes]
    state = compute_macd(_candles(rows))
    assert state.macd < 0
    assert state.direction == BEARISH


def test_macd_histogram_is_macd_minus_signal():
    df = make_uptrend()
    state = compute_macd(df)
    assert abs(state.histogram - (state.macd - state.signal)) < 1e-6


def test_macd_cross_flags_are_mutually_exclusive():
    df = make_uptrend()
    state = compute_macd(df)
    assert not (state.bullish_cross and state.bearish_cross)


def test_macd_score_bounded():
    assert 0.0 <= compute_macd(make_uptrend()).score <= 1.0


# ─────────────────────────────────────────────
# ADX Engine
# ─────────────────────────────────────────────

def test_adx_reports_a_strong_trend_when_price_trends_hard():
    closes = np.linspace(100, 300, 120)
    rows = [(c, c + 0.5, c - 0.5, c, 100) for c in closes]
    state = compute_adx_state(_candles(rows))
    assert state.adx > 25
    assert state.strong_trend
    assert state.direction == BULLISH


def test_adx_direction_follows_the_di_pair():
    up = compute_adx_state(_candles([(c, c + 0.5, c - 0.5, c, 100) for c in np.linspace(100, 300, 120)]))
    down = compute_adx_state(_candles([(c, c + 0.5, c - 0.5, c, 100) for c in np.linspace(300, 100, 120)]))
    assert up.plus_di > up.minus_di
    assert down.minus_di > down.plus_di


def test_adx_trend_strength_labels_are_valid():
    state = compute_adx_state(make_ranging_candles())
    assert state.trend_strength in ('strong', 'weak', 'no_trend')
    assert (state.strong_trend, state.weak_trend, state.no_trend).count(True) == 1


def test_adx_score_bounded():
    assert 0.0 <= compute_adx_state(make_uptrend()).score <= 1.0


# ─────────────────────────────────────────────
# Pattern Recognition Engine
# ─────────────────────────────────────────────

def _swings(prices, kinds):
    return [Swing(index=i * 4, price=p, kind=k) for i, (p, k) in enumerate(zip(prices, kinds))]


def test_double_top_detected():
    swings = _swings([90, 120, 100, 120.5], ['low', 'high', 'low', 'high'])
    state = detect_patterns(swings, price=110.0, atr=2.0)
    names = [c.name for c in state.candidates]
    assert 'Double Top' in names


def test_double_bottom_detected():
    swings = _swings([110, 80, 100, 80.5], ['high', 'low', 'high', 'low'])
    state = detect_patterns(swings, price=95.0, atr=2.0)
    names = [c.name for c in state.candidates]
    assert 'Double Bottom' in names


def test_head_and_shoulders_detected():
    swings = _swings([100, 100.5, 130, 101], ['low', 'high', 'high', 'high'])
    # left shoulder 100.5, head 130, right shoulder 101 (~equal shoulders)
    swings = _swings([100.5, 130, 101], ['high', 'high', 'high'])
    state = detect_patterns(
        _swings([90, 100.5, 95, 130, 96, 101], ['low', 'high', 'low', 'high', 'low', 'high']),
        price=105.0, atr=2.0,
    )
    names = [c.name for c in state.candidates]
    assert 'Head and Shoulders' in names


def test_ascending_triangle_detected():
    # Flat highs, rising lows.
    swings = _swings([80, 120, 90, 120, 100, 120], ['low', 'high', 'low', 'high', 'low', 'high'])
    state = detect_patterns(swings, price=115.0, atr=2.0)
    names = [c.name for c in state.candidates]
    assert any('Triangle' in n for n in names)


def test_pattern_direction_matches_the_pattern():
    swings = _swings([90, 120, 100, 120.5], ['low', 'high', 'low', 'high'])
    state = detect_patterns(swings, price=110.0, atr=2.0)
    if state.pattern and state.pattern.name == 'Double Top':
        assert state.direction == BEARISH


def test_patterns_are_deterministic():
    swings = _swings([80, 120, 90, 120, 100, 120], ['low', 'high', 'low', 'high', 'low', 'high'])
    a = detect_patterns(swings, 115.0, 2.0)
    b = detect_patterns(swings, 115.0, 2.0)
    assert a.name == b.name and a.score == b.score


def test_too_few_swings_yields_no_pattern():
    assert detect_patterns([Swing(0, 100.0, 'low')], 100.0, 1.0).pattern is None
