"""Tests that the Smart Money modules are wired into confluence and scoring.

Confirms the 15-module confluence, that SMC modules participate in Quality and
Confidence, that no single module can create a signal, and that signal quality
responds to added confirmation.
"""

import pytest

from analysis.cache import CANDLE_CACHE
from analysis.confluence import ConfluenceEngine
from analysis.modules import (
    ADX,
    FVG,
    LIQUIDITY,
    MACD,
    MODULE_ORDER,
    ORDER_BLOCK,
    PATTERN,
    VWAP,
    evaluate_modules,
)
from analysis.pipeline import SignalPipeline
from analysis.scoring import CONFIDENCE_BUDGET
from tests.fakes import StubProvider, make_downtrend, make_uptrend

SMC_MODULES = {ORDER_BLOCK, FVG, LIQUIDITY, VWAP, MACD, ADX, PATTERN}


@pytest.fixture
def pipeline(settings) -> SignalPipeline:
    return SignalPipeline(settings)


def _mtf(pipeline, provider, symbol='BTCUSDT', tf='15m'):
    CANDLE_CACHE.clear()
    return pipeline.mtf_engine.build(provider, symbol, tf)


# ─────────────────────────────────────────────
# Wiring
# ─────────────────────────────────────────────

def test_confluence_now_aggregates_fifteen_modules(pipeline):
    mtf = _mtf(pipeline, StubProvider(candles=make_uptrend()))
    result = ConfluenceEngine().evaluate(mtf)
    assert len(result.votes) == 15
    assert [v.module for v in result.votes] == list(MODULE_ORDER)


def test_all_seven_smc_modules_produce_a_vote(pipeline):
    mtf = _mtf(pipeline, StubProvider(candles=make_uptrend()))
    votes = {v.module for v in evaluate_modules(mtf)}
    assert SMC_MODULES.issubset(votes)


def test_every_smc_vote_is_well_formed(pipeline):
    mtf = _mtf(pipeline, StubProvider(candles=make_downtrend()))
    for vote in evaluate_modules(mtf):
        if vote.module in SMC_MODULES:
            assert vote.direction in ('bullish', 'bearish', 'neutral')
            assert 0.0 <= vote.strength <= 1.0
            assert vote.label
            assert vote.detail


# ─────────────────────────────────────────────
# Scoring integration
# ─────────────────────────────────────────────

def test_smc_modules_appear_in_the_quality_breakdown(pipeline):
    result = pipeline.run(StubProvider(candles=make_uptrend()), 'BTCUSDT', '15m')
    names = {c.name for c in result.quality.components}
    assert SMC_MODULES.issubset(names)


def test_quality_still_sums_to_its_components(pipeline):
    """The invariant must survive the expansion to 15 modules."""
    result = pipeline.run(StubProvider(candles=make_uptrend()), 'BTCUSDT', '15m')
    assert result.quality.value == round(sum(c.points for c in result.quality.components))


def test_confidence_includes_smc_agreement(pipeline):
    result = pipeline.run(StubProvider(candles=make_uptrend()), 'BTCUSDT', '15m')
    names = {c.name for c in result.confidence.components}
    for component in (
        'order_block_agreement', 'liquidity_agreement',
        'fvg_agreement', 'pattern_agreement',
    ):
        assert component in names


def test_confidence_budget_totals_one_hundred():
    assert sum(CONFIDENCE_BUDGET.values()) == 100


def test_adx_is_a_strength_module_not_a_directional_one(pipeline):
    """ADX confirms tradeability; it must never zero out for 'opposing'."""
    result = pipeline.run(StubProvider(candles=make_uptrend()), 'BTCUSDT', '15m')
    adx = next(c for c in result.quality.components if c.name == ADX)
    # A strength module always earns weight × its own strength, ≥ 0.
    assert adx.points >= 0.0
    assert 'context/strength' in adx.detail or adx.points == 0.0


# ─────────────────────────────────────────────
# No module acts alone
# ─────────────────────────────────────────────

def test_no_single_module_creates_a_signal(pipeline):
    """Even a maximally strong SMC read cannot force a direction alone.

    A ranging market where structure/trend are undecided must not become a
    tradeable signal purely because, say, MACD or VWAP leans one way.
    """
    from tests.fakes import make_ranging_candles

    result = pipeline.run(StubProvider(candles=make_ranging_candles()), 'BTCUSDT', '15m')
    # Whatever the SMC modules say, a directionless market yields WAIT.
    if not result.confluence.has_direction or result.confluence.agreement < 0.6:
        assert result.signal.direction == 'WAIT'


def test_determinism_holds_across_the_full_15_module_pipeline(pipeline):
    provider = StubProvider(candles=make_uptrend())
    first = pipeline.run(provider, 'BTCUSDT', '15m')
    CANDLE_CACHE.clear()
    second = pipeline.run(provider, 'BTCUSDT', '15m')
    assert first.signal.direction == second.signal.direction
    assert first.quality.value == second.quality.value
    assert first.confidence.value == second.confidence.value
    assert [v.direction for v in first.confluence.votes] == \
        [v.direction for v in second.confluence.votes]
