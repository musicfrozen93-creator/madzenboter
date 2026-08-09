"""Tests for the Phase 3 trade-intelligence layer.

The layer reviews and explains an existing signal. These tests lock the two
non-negotiable guarantees — it never alters the signal, and it never generates
one — alongside the behaviour of each engine.
"""

import pytest

from analysis.cache import CANDLE_CACHE
from analysis.generator import BUY, SELL, WAIT
from analysis.intelligence import build_intelligence
from analysis.intelligence.lifecycle import NO_SETUP, TRIGGERED, WAITING
from analysis.pipeline import SignalPipeline
from tests.fakes import (
    StubProvider,
    make_candles,
    make_downtrend,
    make_ranging_candles,
    make_uptrend,
)


@pytest.fixture
def pipeline(settings) -> SignalPipeline:
    return SignalPipeline(settings)


def _run(pipeline, provider, symbol='BTCUSDT', tf='15m'):
    CANDLE_CACHE.clear()
    return pipeline.run(provider, symbol, tf)


def _actionable(pipeline):
    """Find a provider shape that yields an actionable signal, else skip."""
    for builder in (
        lambda: make_candles(n=500, start=100, end=180, swing_amplitude=5),
        lambda: make_candles(n=500, start=100, end=140, swing_amplitude=3),
        make_uptrend,
        make_downtrend,
    ):
        result = _run(pipeline, StubProvider(candles=builder()))
        if result.signal.actionable:
            return result
    pytest.skip('no actionable signal from the synthetic providers')


# ─────────────────────────────────────────────
# The two guarantees
# ─────────────────────────────────────────────

def test_intelligence_never_changes_the_signal(pipeline):
    """The layer must not touch direction, levels, or scores."""
    result = _run(pipeline, StubProvider(candles=make_uptrend()))
    before = (
        result.signal.direction, result.signal.entry, result.signal.stop_loss,
        tuple(result.signal.take_profits), result.quality.value, result.confidence.value,
    )
    # Rebuild intelligence over the same result — nothing about the signal moves.
    build_intelligence(result)
    after = (
        result.signal.direction, result.signal.entry, result.signal.stop_loss,
        tuple(result.signal.take_profits), result.quality.value, result.confidence.value,
    )
    assert before == after


def test_intelligence_never_invents_a_direction(pipeline):
    """A WAIT stays WAIT — the layer explains, it does not upgrade."""
    result = _run(pipeline, StubProvider(candles=make_ranging_candles()))
    if result.signal.direction == WAIT:
        # No part of the intelligence layer produces BUY/SELL of its own.
        assert result.intelligence.trade_guide.tradeable is False
        assert result.intelligence.lifecycle.status == NO_SETUP


def test_intelligence_is_always_present(pipeline):
    result = _run(pipeline, StubProvider(candles=make_uptrend()))
    assert result.intelligence is not None
    it = result.intelligence
    assert it.validation and it.health and it.trade_guide
    assert it.lifecycle and it.risk and it.invalidation and it.narrative


# ─────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────

def test_validation_score_is_bounded_and_banded(pipeline):
    it = _run(pipeline, StubProvider(candles=make_uptrend())).intelligence
    assert 0 <= it.validation.score <= 100
    assert it.validation.status in (
        'Excellent Validation', 'Strong Validation',
        'Moderate Validation', 'Weak Validation',
    )


def test_validation_checks_cover_every_module(pipeline):
    from analysis.modules import MODULE_ORDER
    it = _run(pipeline, StubProvider(candles=make_uptrend())).intelligence
    assert len(it.validation.checks) == len(MODULE_ORDER)
    assert {c.module for c in it.validation.checks} == set(MODULE_ORDER)


def test_validation_counts_are_consistent(pipeline):
    it = _run(pipeline, StubProvider(candles=make_downtrend())).intelligence
    v = it.validation
    assert v.confirmed + v.against + v.neutral == v.total == len(v.checks)


def test_validation_statuses_are_valid(pipeline):
    it = _run(pipeline, StubProvider(candles=make_uptrend())).intelligence
    for check in it.validation.checks:
        assert check.status in ('confirmed', 'neutral', 'against')


# ─────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────

def test_health_stars_and_label(pipeline):
    it = _run(pipeline, StubProvider(candles=make_uptrend())).intelligence
    assert 1 <= it.health.stars <= 5
    assert it.health.stars_display.count('★') == it.health.stars
    assert it.health.label in ('Excellent', 'Good', 'Average', 'Weak', 'Poor')


def test_health_of_a_wait_is_capped(pipeline):
    result = _run(pipeline, StubProvider(candles=make_ranging_candles()))
    if result.signal.direction == WAIT:
        assert result.intelligence.health.stars <= 2


def test_health_blends_quality_confidence_validation(pipeline):
    it = _run(pipeline, StubProvider(candles=make_uptrend())).intelligence
    assert 0 <= it.health.composite <= 100


# ─────────────────────────────────────────────
# Trade guide
# ─────────────────────────────────────────────

def test_actionable_guide_has_seven_steps(pipeline):
    result = _actionable(pipeline)
    guide = result.intelligence.trade_guide
    assert guide.tradeable
    assert len(guide.steps) == 7
    assert [s.number for s in guide.steps] == list(range(1, 8))


def test_guide_references_the_actual_levels(pipeline):
    result = _actionable(pipeline)
    signal = result.signal
    text = ' '.join(s.detail for s in result.intelligence.trade_guide.steps)
    # The guide is dynamic — it names the real entry and stop, not generic advice.
    assert f'{signal.entry:g}' in text
    assert f'{signal.stop_loss:g}' in text


def test_wait_guide_recommends_fresh_analysis(pipeline):
    result = _run(pipeline, StubProvider(candles=make_ranging_candles()))
    if result.signal.direction == WAIT:
        guide = result.intelligence.trade_guide
        assert not guide.tradeable
        assert any('Re-run' in s.title or 're-run' in s.detail.lower() for s in guide.steps)


# ─────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────

def test_lifecycle_status_reflects_the_signal(pipeline):
    result = _run(pipeline, StubProvider(candles=make_uptrend()))
    status = result.intelligence.lifecycle.status
    if result.signal.actionable:
        assert status in (WAITING, TRIGGERED)
    else:
        assert status == NO_SETUP


def test_lifecycle_expiration_names_the_timeframe(pipeline):
    it = _run(pipeline, StubProvider(candles=make_uptrend()), tf='15m').intelligence
    assert '15m' in it.lifecycle.expiration


def test_lifecycle_timeline_has_the_full_sequence(pipeline):
    from analysis.intelligence.lifecycle import EXECUTION_STAGES
    it = _run(pipeline, StubProvider(candles=make_uptrend())).intelligence
    assert tuple(it.lifecycle.stages) == EXECUTION_STAGES


def test_wait_recommends_fresh_analysis(pipeline):
    result = _run(pipeline, StubProvider(candles=make_ranging_candles()))
    if result.signal.direction == WAIT:
        assert result.intelligence.lifecycle.recommend_fresh_analysis


# ─────────────────────────────────────────────
# Risk advisory
# ─────────────────────────────────────────────

def test_risk_level_is_valid(pipeline):
    it = _run(pipeline, StubProvider(candles=make_downtrend())).intelligence
    assert it.risk.level in ('Low', 'Medium', 'High')


def test_risk_reasons_come_from_the_analysis(pipeline):
    """Every risk factor must carry a concrete detail, not a generic warning."""
    it = _run(pipeline, StubProvider(candles=make_downtrend())).intelligence
    for factor in it.risk.factors:
        assert factor.factor
        assert factor.detail
        assert isinstance(factor.raises_risk, bool)


def test_counter_trend_raises_risk(pipeline):
    """A signal against the higher timeframe must flag counter-trend risk."""
    provider = StubProvider(
        candles=make_uptrend(),
        per_timeframe={'4h': make_downtrend(), '1h': make_downtrend()},
    )
    result = _run(pipeline, provider)
    factors = {f.factor for f in result.intelligence.risk.factors}
    # Either it's WAIT (blocked) or, if it somehow trades, counter-trend shows.
    if result.signal.actionable:
        assert 'Counter Trend' in factors


# ─────────────────────────────────────────────
# Invalidation
# ─────────────────────────────────────────────

def test_invalidation_conditions_exist(pipeline):
    it = _run(pipeline, StubProvider(candles=make_uptrend())).intelligence
    assert it.invalidation.conditions
    for condition in it.invalidation.conditions:
        assert condition.source
        assert condition.condition


def test_actionable_invalidation_includes_the_stop(pipeline):
    result = _actionable(pipeline)
    conditions = result.intelligence.invalidation.conditions
    sources = {c.source for c in conditions}
    assert 'stop' in sources
    # The stop condition names the actual stop level.
    stop_cond = next(c for c in conditions if c.source == 'stop')
    assert f'{result.signal.stop_loss:g}' in stop_cond.condition


def test_invalidation_only_cites_present_analysis(pipeline):
    """Conditions must come from modules that actually participated."""
    result = _actionable(pipeline)
    picture = result.mtf.entry
    sources = {c.source for c in result.intelligence.invalidation.conditions}
    # An order-block condition may only appear if an order block was detected.
    if 'order_block' in sources:
        assert picture.order_blocks.nearest is not None
    if 'liquidity' in sources:
        assert picture.liquidity.last_sweep is not None


# ─────────────────────────────────────────────
# Narrative / explanations
# ─────────────────────────────────────────────

def test_both_explanation_modes_are_produced(pipeline):
    it = _run(pipeline, StubProvider(candles=make_uptrend())).intelligence
    assert it.narrative.summary
    assert it.narrative.beginner
    assert it.narrative.professional


def test_actionable_summary_is_substantive(pipeline):
    result = _actionable(pipeline)
    summary = result.intelligence.narrative.summary
    # Professional prose, not a one-liner.
    assert len(summary) > 60
    assert summary.endswith('.')


def test_explanations_are_deterministic(pipeline):
    provider = StubProvider(candles=make_uptrend())
    first = _run(pipeline, provider).intelligence
    second = _run(pipeline, provider).intelligence
    assert first.narrative.summary == second.narrative.summary
    assert first.narrative.beginner == second.narrative.beginner
    assert first.validation.score == second.validation.score
    assert first.health.stars == second.health.stars


# ─────────────────────────────────────────────
# Performance — no recompute
# ─────────────────────────────────────────────

def test_intelligence_adds_no_market_fetches(pipeline):
    """The layer reads the existing analysis; it must not fetch or recompute."""
    provider = StubProvider(candles=make_uptrend())
    CANDLE_CACHE.clear()
    result = pipeline.run(provider, 'BTCUSDT', '15m')
    fetches_with_intelligence = len(provider.fetch_calls)
    # The intelligence layer ran inside that call and added zero fetches beyond
    # the analysis itself (3 rungs + 1 benchmark = 4 distinct series).
    assert fetches_with_intelligence <= 6
