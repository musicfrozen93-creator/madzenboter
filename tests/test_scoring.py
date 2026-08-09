"""Tests for the Quality and Confidence scoring engines.

Both must be deterministic, fully explainable, and never null. Every point has to
trace back to a stated rule.
"""

import pytest

from analysis.cache import CANDLE_CACHE
from analysis.confluence import ConfluenceEngine
from analysis.engine import AnalysisEngine
from analysis.modules import MODULE_ORDER, MODULE_WEIGHTS
from analysis.scoring import (
    CONFIDENCE_BUDGET,
    QUALITY_BANDS,
    ConfidenceScorer,
    QualityScorer,
    grade_for,
)
from analysis.timeframes import MultiTimeframeEngine
from tests.fakes import StubProvider, make_downtrend, make_ranging_candles, make_uptrend


@pytest.fixture
def scored(settings):
    """Score a provider and return (quality, confidence, confluence, mtf)."""
    def _score(provider, symbol='BTCUSDT', timeframe='15m'):
        # Every stub shares the provider name 'stub', so without this a second
        # provider in the same test would be served the first one's candles.
        CANDLE_CACHE.clear()
        mtf = MultiTimeframeEngine(settings, AnalysisEngine(settings)).build(
            provider, symbol, timeframe
        )
        confluence = ConfluenceEngine().evaluate(mtf)
        quality = QualityScorer().score(mtf, confluence)
        confidence = ConfidenceScorer().score(mtf, confluence)
        return quality, confidence, confluence, mtf
    return _score


# ─────────────────────────────────────────────
# Weights and grading
# ─────────────────────────────────────────────

def test_module_weights_total_one_hundred():
    assert sum(MODULE_WEIGHTS.values()) == 100


def test_the_specified_weighting_is_used():
    # Phase 2 — the 15-module weighting (8 classical + 7 Smart Money).
    assert MODULE_WEIGHTS == {
        'trend': 15, 'structure': 15, 'elliott': 10, 'fibonacci': 8,
        'volume': 8, 'rsi': 6, 'atr': 4, 'support_resistance': 8,
        'order_block': 8, 'fvg': 5, 'liquidity': 5, 'vwap': 3,
        'macd': 3, 'adx': 1, 'pattern': 1,
    }


def test_all_fifteen_modules_are_present():
    from analysis.modules import MODULE_ORDER
    assert len(MODULE_ORDER) == 15
    assert len(MODULE_WEIGHTS) == 15


@pytest.mark.parametrize('value,expected', [
    (100, 'Elite Setup'), (95, 'Elite Setup'),
    (94, 'High Quality'), (90, 'High Quality'),
    (89, 'Good'), (80, 'Good'),
    (79, 'Average'), (70, 'Average'),
    (69, 'Weak'), (0, 'Weak'),
])
def test_quality_grading_bands_match_the_specification(value, expected):
    assert grade_for(value) == expected


def test_grading_bands_are_ordered_and_complete():
    thresholds = [t for t, _ in QUALITY_BANDS]
    assert thresholds == sorted(thresholds, reverse=True)
    assert thresholds[-1] == 0          # every score falls into a band


def test_confidence_budget_totals_one_hundred():
    assert sum(CONFIDENCE_BUDGET.values()) == 100


# ─────────────────────────────────────────────
# Quality Score
# ─────────────────────────────────────────────

def test_quality_is_always_an_integer_never_null(scored):
    quality, _, _, _ = scored(StubProvider(candles=make_uptrend()))
    assert isinstance(quality.value, int)
    assert 0 <= quality.value <= 100
    assert quality.grade


def test_quality_reports_one_component_per_module(scored):
    quality, _, _, _ = scored(StubProvider(candles=make_uptrend()))
    assert [c.name for c in quality.components] == list(MODULE_ORDER)


def test_every_component_stays_within_its_budget(scored):
    quality, _, _, _ = scored(StubProvider(candles=make_uptrend()))
    for component in quality.components:
        assert 0 <= component.points <= component.max_points
        assert component.max_points == MODULE_WEIGHTS[component.name]


def test_the_total_is_the_sum_of_its_components(scored):
    """Every point must come from a rule — no unexplained score."""
    quality, _, _, _ = scored(StubProvider(candles=make_uptrend()))
    assert quality.value == round(sum(c.points for c in quality.components))


def test_every_component_explains_itself(scored):
    quality, _, _, _ = scored(StubProvider(candles=make_uptrend()))
    for component in quality.components:
        assert component.label
        assert component.detail
    assert len(quality.factors) == len(MODULE_ORDER)


def test_an_aligned_trend_scores_higher_than_chop(scored):
    aligned, _, _, _ = scored(StubProvider(candles=make_uptrend()))
    chop, _, _, _ = scored(StubProvider(candles=make_ranging_candles()))
    assert aligned.value > chop.value


def test_a_module_opposing_the_setup_contributes_nothing(scored):
    quality, _, confluence, _ = scored(StubProvider(candles=make_uptrend()))
    side = confluence.trade_side
    if side is None:
        pytest.skip('no direction to test opposition against')
    by_name = {c.name: c for c in quality.components}
    for vote in confluence.votes:
        if vote.module != 'atr' and vote.opposes(side):
            assert by_name[vote.module].points == 0.0


def test_quality_is_deterministic(scored):
    provider = StubProvider(candles=make_uptrend())
    first, _, _, _ = scored(provider)
    second, _, _, _ = scored(provider)
    assert first.value == second.value
    assert [c.points for c in first.components] == [c.points for c in second.components]


# ─────────────────────────────────────────────
# Confidence Score
# ─────────────────────────────────────────────

def test_confidence_is_always_an_integer_never_null(scored):
    _, confidence, _, _ = scored(StubProvider(candles=make_uptrend()))
    assert isinstance(confidence.value, int)
    assert 0 <= confidence.value <= 100
    assert confidence.grade


def test_confidence_covers_every_specified_factor(scored):
    _, confidence, _, _ = scored(StubProvider(candles=make_uptrend()))
    names = {c.name for c in confidence.components}
    assert names == set(CONFIDENCE_BUDGET) | {'conflict_penalty'}


def test_confidence_is_higher_when_the_timeframes_agree(scored):
    _, agreeing, _, _ = scored(StubProvider(candles=make_uptrend()))
    _, conflicting, _, _ = scored(StubProvider(
        candles=make_uptrend(),
        per_timeframe={'4h': make_downtrend(), '1h': make_downtrend()},
    ))
    assert agreeing.value > conflicting.value


def test_conflicts_subtract_from_confidence(scored):
    _, confidence, _, _ = scored(StubProvider(
        candles=make_uptrend(),
        per_timeframe={'4h': make_downtrend(), '1h': make_downtrend()},
    ))
    penalty = next(c for c in confidence.components if c.name == 'conflict_penalty')
    assert penalty.points <= 0


def test_missing_timeframes_lower_data_quality(scored):
    from tests.fakes import make_candles

    _, complete, _, _ = scored(StubProvider(candles=make_uptrend()))
    # A pair with no 4h history: the trend rung drops out of the ladder.
    _, degraded, _, _ = scored(StubProvider(
        candles=make_uptrend(), per_timeframe={'4h': make_candles(n=30)},
    ))
    complete_dq = next(c for c in complete.components if c.name == 'data_quality')
    degraded_dq = next(c for c in degraded.components if c.name == 'data_quality')
    assert degraded_dq.points < complete_dq.points


def test_confidence_is_deterministic(scored):
    provider = StubProvider(candles=make_uptrend())
    _, first, _, _ = scored(provider)
    _, second, _, _ = scored(provider)
    assert first.value == second.value


def test_quality_and_confidence_measure_different_things(scored):
    """They must not be the same number wearing two hats."""
    quality, confidence, _, _ = scored(StubProvider(candles=make_uptrend()))
    assert {c.name for c in quality.components} != {c.name for c in confidence.components}
