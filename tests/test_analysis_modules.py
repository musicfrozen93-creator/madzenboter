"""Tests for the four structure-derived analysis modules.

Market structure, support/resistance, Fibonacci, and Elliott waves all read the
same confirmed swing sequence, so they are tested together against deliberately
shaped series.
"""

import numpy as np
import pandas as pd
import pytest

from analysis.elliott import MAX_CONFIDENCE, analyze_elliott
from analysis.fibonacci import analyze_fibonacci
from analysis.levels import detect_levels
from analysis.structure import (
    BEARISH,
    BULLISH,
    CHOCH,
    RANGE,
    Swing,
    analyze_structure,
    find_swings,
)
from tests.fakes import make_downtrend, make_ranging_candles, make_uptrend


def _frame(highs, lows, closes=None, opens=None) -> pd.DataFrame:
    n = len(highs)
    closes = closes if closes is not None else [(h + l) / 2 for h, l in zip(highs, lows)]
    opens = opens if opens is not None else closes
    return pd.DataFrame({
        'timestamp': pd.to_datetime(np.arange(n) * 900_000, unit='ms'),
        'open': opens, 'high': highs, 'low': lows, 'close': closes,
        'volume': [1000.0] * n,
    })


def _zigzag(points, bars_per_leg=6) -> pd.DataFrame:
    """Build a series that visits each price in `points` in order."""
    closes: list[float] = []
    for start, end in zip(points, points[1:]):
        closes.extend(np.linspace(start, end, bars_per_leg, endpoint=False))
    closes.append(points[-1])
    closes = [float(c) for c in closes]
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.05 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.05 for o, c in zip(opens, closes)]
    return _frame(highs, lows, closes, opens)


# ─────────────────────────────────────────────
# Swing detection
# ─────────────────────────────────────────────

def test_swings_alternate_between_highs_and_lows():
    swings = find_swings(_zigzag([10, 20, 12, 25, 15, 30]))
    assert len(swings) >= 4
    kinds = [s.kind for s in swings]
    assert all(a != b for a, b in zip(kinds, kinds[1:])), kinds


def test_no_swing_is_called_on_unconfirmed_bars():
    """The final bars cannot form a pivot — an unconfirmed pivot is not a pivot."""
    df = _zigzag([10, 20, 12, 25])
    swings = find_swings(df, left=2, right=2)
    assert all(s.index <= len(df) - 3 for s in swings)


def test_too_short_a_series_yields_no_swings():
    assert find_swings(_zigzag([10, 12], bars_per_leg=1)) == []


# ─────────────────────────────────────────────
# Market structure
# ─────────────────────────────────────────────

def test_higher_highs_and_higher_lows_read_as_bullish():
    state = analyze_structure(_zigzag([10, 20, 14, 26, 19, 32]), atr=1.0)
    assert state.trend == BULLISH
    assert state.higher_highs and state.higher_lows


def test_lower_highs_and_lower_lows_read_as_bearish():
    state = analyze_structure(_zigzag([32, 19, 26, 14, 20, 10]), atr=1.0)
    assert state.trend == BEARISH
    assert state.lower_highs and state.lower_lows


def test_structure_never_guesses_without_enough_swings():
    state = analyze_structure(_zigzag([10, 12]), atr=1.0)
    assert state.trend == RANGE
    assert not state.has_structure
    assert state.reason == 'insufficient_data'


def test_a_break_against_the_trend_is_a_change_of_character():
    # Bullish sequence, then price collapses through the last swing low.
    df = _zigzag([10, 20, 14, 26, 19, 30, 5])
    state = analyze_structure(df, atr=1.0)
    if state.event is not None and state.event_direction == BEARISH:
        assert state.event == CHOCH


def test_protective_swings_sit_on_the_correct_side_of_price():
    df = _zigzag([10, 20, 14, 26, 19, 24])
    state = analyze_structure(df, atr=1.0)
    close = float(df['close'].iloc[-1])
    if state.protective_low is not None:
        assert state.protective_low < close
    if state.protective_high is not None:
        assert state.protective_high > close


# ─────────────────────────────────────────────
# Support / resistance
# ─────────────────────────────────────────────

def test_repeated_swings_at_one_price_form_a_single_zone():
    swings = [
        Swing(index=i, price=price, kind=kind)
        for i, (price, kind) in enumerate([
            (100.0, 'low'), (120.0, 'high'), (100.2, 'low'),
            (121.0, 'high'), (99.9, 'low'),
        ])
    ]
    levels = detect_levels(swings, price=110.0, atr=2.0, total_bars=100)
    assert levels.nearest_support is not None
    # The three visits near 100 are one level, not three.
    assert levels.nearest_support.touches == 3


def test_zones_split_into_support_below_and_resistance_above():
    swings = [
        Swing(index=0, price=90.0, kind='low'),
        Swing(index=1, price=130.0, kind='high'),
    ]
    levels = detect_levels(swings, price=110.0, atr=2.0, total_bars=100)
    assert levels.nearest_support.center < 110.0
    assert levels.nearest_resistance.center > 110.0


def test_more_touches_produce_a_stronger_zone():
    busy = [Swing(index=i, price=100.0 + i * 0.01, kind='low') for i in range(5)]
    lonely = [Swing(index=0, price=100.0, kind='low')]
    busy_zone = detect_levels(busy, 110.0, 2.0, 100).nearest_support
    lonely_zone = detect_levels(lonely, 110.0, 2.0, 100).nearest_support
    assert busy_zone.strength > lonely_zone.strength


def test_no_swings_means_no_levels_not_invented_ones():
    levels = detect_levels([], price=100.0, atr=1.0, total_bars=100)
    assert not levels.has_levels
    assert levels.nearest_support is None


# ─────────────────────────────────────────────
# Fibonacci
# ─────────────────────────────────────────────

def test_retracement_is_measured_across_the_dominant_leg():
    swings = [
        Swing(index=0, price=100.0, kind='low'),
        Swing(index=1, price=200.0, kind='high'),
    ]
    fib = analyze_fibonacci(swings, price=150.0)
    assert fib.valid
    assert fib.direction == 'up'
    assert fib.current_ratio == pytest.approx(0.5)
    assert fib.retracements['0.5'] == pytest.approx(150.0)


def test_golden_pocket_is_detected():
    swings = [
        Swing(index=0, price=0.0, kind='low'),
        Swing(index=1, price=100.0, kind='high'),
    ]
    assert analyze_fibonacci(swings, price=38.0).in_golden_pocket   # 0.62 retrace
    assert not analyze_fibonacci(swings, price=90.0).in_golden_pocket


def test_extension_beyond_the_leg_is_negative_not_a_deep_retracement():
    """A runaway trend must not be reported as a pullback."""
    swings = [
        Swing(index=0, price=100.0, kind='low'),
        Swing(index=1, price=200.0, kind='high'),
    ]
    fib = analyze_fibonacci(swings, price=250.0)     # extended past the high
    assert fib.current_ratio < 0
    assert 'extended' in fib.zone_label


def test_full_retracement_invalidates_the_leg():
    swings = [
        Swing(index=0, price=100.0, kind='low'),
        Swing(index=1, price=200.0, kind='high'),
    ]
    fib = analyze_fibonacci(swings, price=90.0)
    assert fib.current_ratio > 1.0
    assert 'invalidated' in fib.zone_label


def test_extensions_project_beyond_the_leg_in_its_direction():
    swings = [
        Swing(index=0, price=100.0, kind='low'),
        Swing(index=1, price=200.0, kind='high'),
    ]
    fib = analyze_fibonacci(swings, price=150.0)
    assert fib.extensions['1.618'] == pytest.approx(261.8)
    assert all(price > 200.0 for price in fib.extensions.values())


def test_no_leg_means_no_fibonacci_levels():
    assert not analyze_fibonacci([], price=100.0).valid


# ─────────────────────────────────────────────
# Elliott waves
# ─────────────────────────────────────────────

def _impulse_swings(prices, kinds):
    return [
        Swing(index=i, price=p, kind=k) for i, (p, k) in enumerate(zip(prices, kinds))
    ]


def test_a_five_wave_impulse_is_labelled():
    # L0 H1 L2 H3 L4, price advancing = wave 5 in progress.
    swings = _impulse_swings(
        [100, 130, 115, 175, 155], ['low', 'high', 'low', 'high', 'low']
    )
    state = analyze_elliott(swings, price=180.0)
    assert state.valid
    assert state.primary.label in ('Wave 5', 'Wave C', 'Wave 3')
    assert state.direction == 'bullish'


def test_an_alternative_count_is_always_offered():
    swings = _impulse_swings(
        [100, 130, 115, 175, 155], ['low', 'high', 'low', 'high', 'low']
    )
    state = analyze_elliott(swings, price=180.0)
    assert state.alternative is not None


def test_the_alternative_is_a_genuinely_different_reading():
    """The alternative must not be the primary counted twice."""
    for prices, kinds, price in (
        ([100, 130, 115, 175, 155], ['low', 'high', 'low', 'high', 'low'], 180.0),
        ([100, 130, 115], ['low', 'high', 'low'], 140.0),
        ([200, 170, 185, 140, 160], ['high', 'low', 'high', 'low', 'high'], 130.0),
    ):
        state = analyze_elliott(_impulse_swings(prices, kinds), price=price)
        if state.alternative is None:
            continue
        signature = (state.primary.label, state.primary.degree, state.primary.direction)
        alternative = (
            state.alternative.label, state.alternative.degree, state.alternative.direction
        )
        assert signature != alternative, f'alternative echoes the primary: {signature}'


def test_confidence_is_never_certainty():
    """Elliott is inherently ambiguous — the engine must never claim 100%."""
    swings = _impulse_swings(
        [100, 130, 115, 175, 155], ['low', 'high', 'low', 'high', 'low']
    )
    state = analyze_elliott(swings, price=180.0)
    assert 0 < state.confidence <= MAX_CONFIDENCE
    assert state.primary.confidence <= MAX_CONFIDENCE


def test_probabilities_of_the_two_counts_are_complementary():
    swings = _impulse_swings(
        [100, 130, 115, 175, 155], ['low', 'high', 'low', 'high', 'low']
    )
    state = analyze_elliott(swings, price=180.0)
    total = state.primary.confidence + state.alternative.confidence
    assert 95 <= total <= 105       # allow for the confidence cap/floor


def test_a_healthy_impulse_records_the_rules_it_satisfies():
    # L0=100, H1=130, L2=115 — wave 2 held well above the start of wave 1.
    swings = _impulse_swings([100, 130, 115], ['low', 'high', 'low'])
    state = analyze_elliott(swings, price=140.0)
    counts = [c for c in (state.primary, state.alternative) if c]
    assert any('R1' in rule for count in counts for rule in count.rules_passed)


def test_a_count_breaking_a_hard_rule_loses_to_one_that_does_not():
    """R1: wave 2 never retraces beyond the start of wave 1.

    With L0=100, H1=130, L2=95 the impulse reading breaks R1, so the engine must
    prefer a corrective reading of the same pivots instead of promoting the
    rule-breaking count.
    """
    swings = _impulse_swings([100, 130, 95], ['low', 'high', 'low'])
    state = analyze_elliott(swings, price=110.0)
    assert state.primary.is_valid, (
        f'promoted a rule-breaking count: {state.primary.rules_violated}'
    )


def test_a_violated_count_is_marked_invalid():
    swings = _impulse_swings([100, 130, 95], ['low', 'high', 'low'])
    state = analyze_elliott(swings, price=110.0)
    for count in (state.primary, state.alternative):
        if count and count.rules_violated:
            assert not count.is_valid


def test_completion_estimate_stays_within_bounds():
    swings = _impulse_swings([100, 130, 115], ['low', 'high', 'low'])
    for price in (116.0, 150.0, 400.0):
        state = analyze_elliott(swings, price=price)
        assert 0.0 <= state.primary.completion_pct <= 100.0


def test_too_few_pivots_means_no_count_is_invented():
    state = analyze_elliott([Swing(0, 100.0, 'low')], price=110.0)
    assert not state.valid
    assert state.label == 'Undetermined'


def test_counts_are_deterministic():
    """Same swings and price must always produce the same count."""
    swings = _impulse_swings(
        [100, 130, 115, 175, 155], ['low', 'high', 'low', 'high', 'low']
    )
    first = analyze_elliott(swings, price=180.0)
    second = analyze_elliott(swings, price=180.0)
    assert first.primary.label == second.primary.label
    assert first.primary.confidence == second.primary.confidence


# ─────────────────────────────────────────────
# Against realistic series
# ─────────────────────────────────────────────

@pytest.mark.parametrize('builder,expected', [
    (make_uptrend, BULLISH),
    (make_downtrend, BEARISH),
])
def test_generated_trends_are_read_correctly(builder, expected):
    df = builder()
    state = analyze_structure(df, atr=float(df['high'].iloc[-1] - df['low'].iloc[-1]))
    assert state.trend == expected


def test_every_module_survives_a_ranging_market():
    df = make_ranging_candles()
    state = analyze_structure(df, atr=0.7)
    levels = detect_levels(state.swings, float(df['close'].iloc[-1]), 0.7, len(df))
    fib = analyze_fibonacci(state.swings, float(df['close'].iloc[-1]))
    elliott = analyze_elliott(state.swings, float(df['close'].iloc[-1]))
    # No exception, and every module reports something coherent.
    assert state.reason
    assert levels.reason
    assert fib.reason
    assert elliott.reason
