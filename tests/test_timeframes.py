"""Tests for the multi-timeframe engine.

The user selects ONE timeframe; the engine analyses three. These tests lock the
ladder mapping, the graceful degradation, the agreement maths, and — critically —
that the internal timeframes never leak into the user-facing result.
"""

import pytest

from analysis.cache import CANDLE_CACHE, RequestScope
from analysis.engine import AnalysisEngine
from analysis.structure import BEARISH, BULLISH, RANGE
from analysis.timeframes import (
    ENTRY,
    STRUCTURE,
    TIMEFRAME_LADDER,
    TREND,
    MultiTimeframeEngine,
    build_ladder,
)
from providers.base import TIMEFRAMES
from tests.fakes import StubProvider, make_candles, make_downtrend, make_uptrend

ALL = tuple(TIMEFRAMES)


# ─────────────────────────────────────────────
# The ladder
# ─────────────────────────────────────────────

@pytest.mark.parametrize('selected,trend,structure', [
    ('15m', '4h', '1h'),        # the three examples from the specification
    ('1h', '1d', '4h'),
    ('4h', '1w', '1d'),
])
def test_specified_ladders(selected, trend, structure):
    ladder = build_ladder(selected, ALL)
    assert ladder.selected == selected
    assert dict((role, tf) for role, tf in ladder.rungs) == {
        TREND: trend, STRUCTURE: structure, ENTRY: selected,
    }


def test_every_selectable_timeframe_has_a_ladder():
    for timeframe in TIMEFRAMES:
        assert timeframe in TIMEFRAME_LADDER, timeframe
        ladder = build_ladder(timeframe, ALL)
        assert ladder.rungs[-1] == (ENTRY, timeframe)


def test_rungs_are_ordered_highest_timeframe_first():
    ladder = build_ladder('15m', ALL)
    assert [tf for _, tf in ladder.rungs] == ['4h', '1h', '15m']


def test_the_top_timeframe_degrades_to_a_single_rung():
    """1w has nothing above it — the ladder must not duplicate it."""
    ladder = build_ladder('1w', ALL)
    assert ladder.rungs == ((ENTRY, '1w'),)
    assert not ladder.is_multi


def test_a_venue_without_the_higher_timeframes_drops_those_rungs():
    ladder = build_ladder('15m', ('15m', '1h'))
    assert [tf for _, tf in ladder.rungs] == ['1h', '15m']       # no 4h offered


# ─────────────────────────────────────────────
# Building the picture
# ─────────────────────────────────────────────

@pytest.fixture
def engine(settings) -> MultiTimeframeEngine:
    return MultiTimeframeEngine(settings, AnalysisEngine(settings))


def test_three_timeframes_are_analysed_for_one_selection(engine):
    mtf = engine.build(StubProvider(), 'BTCUSDT', '15m')
    assert len(mtf.views) == 3
    assert {v.role for v in mtf.views} == {TREND, STRUCTURE, ENTRY}
    assert {v.timeframe for v in mtf.views} == {'4h', '1h', '15m'}


def test_the_user_facing_picture_is_the_selected_timeframe(engine):
    mtf = engine.build(StubProvider(), 'BTCUSDT', '15m')
    assert mtf.selected_timeframe == '15m'
    assert mtf.entry.timeframe == '15m'


def test_aligned_trends_report_full_alignment(engine):
    mtf = engine.build(StubProvider(candles=make_uptrend()), 'BTCUSDT', '15m')
    assert mtf.aligned_direction == BULLISH
    assert mtf.alignment == 1.0
    assert mtf.conflicts == []


def test_a_conflicting_higher_timeframe_is_reported(engine):
    """15m bullish while 4h and 1h are bearish must surface as a conflict."""
    provider = StubProvider(
        candles=make_uptrend(),
        per_timeframe={'4h': make_downtrend(), '1h': make_downtrend()},
    )
    mtf = engine.build(provider, 'BTCUSDT', '15m')

    assert mtf.higher_timeframe_direction == BEARISH
    assert mtf.aligned_direction == BEARISH        # the HTF carries more weight
    # The 15m rung disagrees with the ladder majority, and says so.
    assert mtf.conflicts
    assert mtf.opposes('short') == ['entry timeframe is bullish']
    assert len(mtf.opposes('long')) == 2           # both higher rungs fight a long


def test_agreement_is_weighted_toward_the_higher_timeframe(engine):
    provider = StubProvider(
        candles=make_uptrend(),
        per_timeframe={'4h': make_downtrend(), '1h': make_downtrend()},
    )
    mtf = engine.build(provider, 'BTCUSDT', '15m')
    # The entry rung alone carries 0.20 of the ladder weight.
    assert mtf.agreement_with('long') == pytest.approx(0.20, abs=0.01)
    assert mtf.agreement_with('short') == pytest.approx(0.80, abs=0.01)


def test_a_rung_without_enough_history_degrades_instead_of_failing(engine):
    """A pair listed only recently has no 4h history — analyse what exists."""
    provider = StubProvider(
        candles=make_uptrend(),
        per_timeframe={'4h': make_candles(n=30)},   # far too short
    )
    mtf = engine.build(provider, 'BTCUSDT', '15m')

    assert len(mtf.views) == 2                      # 4h dropped
    assert any('trend' in note for note in mtf.degraded_rungs)
    assert mtf.entry.timeframe == '15m'             # the selection still worked


def test_the_entry_rung_failing_is_fatal(engine):
    """Without the selected timeframe there is nothing to analyse."""
    provider = StubProvider(candles=make_candles(n=30))
    with pytest.raises(ValueError):
        engine.build(provider, 'BTCUSDT', '15m')


def test_a_single_rung_ladder_still_produces_a_picture(engine):
    provider = StubProvider(symbols=('BTCUSDT',))
    mtf = engine.build(provider, 'BTCUSDT', '1w')
    assert len(mtf.views) == 1
    assert mtf.entry.timeframe == '1w'
    assert not mtf.ladder.is_multi


# ─────────────────────────────────────────────
# Caching / performance
# ─────────────────────────────────────────────

def test_each_timeframe_is_fetched_once_per_request(engine):
    provider = StubProvider()
    scope = RequestScope()
    engine.build(provider, 'BTCUSDT', '15m', scope=scope)

    # 3 rungs + 1 benchmark fetch on the trend rung = 4 distinct series.
    assert scope.fetches <= 4
    pairs = provider.fetch_calls
    assert len(pairs) == len(set(pairs)), f'duplicate fetches: {pairs}'


def test_the_benchmark_is_only_fetched_on_the_highest_rung(engine):
    """Systemic context is a higher-timeframe concept — one fetch, not three."""
    provider = StubProvider()
    engine.build(provider, 'ETHUSDT', '15m')       # benchmark is BTCUSDT
    benchmark_calls = [c for c in provider.fetch_calls if c[0] == 'BTCUSDT']
    assert len(benchmark_calls) == 1
    assert benchmark_calls[0][1] == '4h'           # the trend rung


def test_a_repeated_request_is_served_from_cache(engine):
    provider = StubProvider()
    engine.build(provider, 'BTCUSDT', '15m')
    calls_after_first = len(provider.fetch_calls)

    engine.build(provider, 'BTCUSDT', '15m')
    assert len(provider.fetch_calls) == calls_after_first, 'cache did not serve the repeat'
    assert CANDLE_CACHE.stats.hits > 0
