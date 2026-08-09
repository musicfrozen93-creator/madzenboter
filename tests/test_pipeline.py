"""Tests for the assembled pipeline: analysis → confluence → signal.

These lock the decision rules the product depends on — most importantly that
WAIT is preferred whenever the evidence conflicts, and that an actionable signal
always carries a complete, correctly-ordered set of levels.
"""

import pytest

from analysis.confluence import MIN_AGREEMENT, ConfluenceEngine
from analysis.engine import AnalysisEngine
from analysis.generator import BUY, SELL, WAIT
from analysis.modules import MODULE_ORDER
from analysis.pipeline import SignalPipeline
from analysis.scoring import MIN_TRADEABLE_QUALITY
from providers.base import UnknownSymbolError, UnsupportedTimeframeError
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


def _conflicting_provider() -> StubProvider:
    """Entry timeframe bullish, both higher timeframes bearish."""
    return StubProvider(
        candles=make_uptrend(),
        per_timeframe={'4h': make_downtrend(), '1h': make_downtrend()},
    )


# ─────────────────────────────────────────────
# Analysis engine
# ─────────────────────────────────────────────

def test_engine_computes_every_module(settings):
    picture = AnalysisEngine(settings).analyze(StubProvider(), 'BTCUSDT', '15m')

    assert picture.indicators.price > 0
    assert 0 <= picture.indicators.rsi <= 100
    assert picture.structure is not None
    assert picture.levels is not None
    assert picture.fibonacci is not None
    assert picture.elliott is not None
    assert len(picture.quality_filters) == 4


def test_engine_rejects_insufficient_history(settings):
    thin = StubProvider(candles=make_candles(n=40))
    with pytest.raises(ValueError, match='candles available'):
        AnalysisEngine(settings).analyze(thin, 'BTCUSDT', '15m')


def test_engine_propagates_unknown_symbol(settings):
    with pytest.raises(UnknownSymbolError):
        AnalysisEngine(settings).analyze(StubProvider(), 'DOGEUSDT', '15m')


def test_engine_propagates_unsupported_timeframe(settings):
    with pytest.raises(UnsupportedTimeframeError):
        AnalysisEngine(settings).analyze(StubProvider(), 'BTCUSDT', '3d')


def test_engine_survives_a_missing_quote(settings, monkeypatch):
    provider = StubProvider()
    monkeypatch.setattr(
        provider, 'fetch_quote',
        lambda symbol: (_ for _ in ()).throw(RuntimeError('ticker down')),
    )
    picture = AnalysisEngine(settings).analyze(provider, 'BTCUSDT', '15m')
    assert picture.quote is None
    assert picture.indicators.price > 0


def test_wide_spread_marks_conditions_unclean(settings):
    engine = AnalysisEngine(settings)
    assert engine.analyze(StubProvider(spread=0.0), 'BTCUSDT', '15m').market_conditions_clean
    unclean = engine.analyze(StubProvider(spread=500.0), 'BTCUSDT', '15m')
    assert not unclean.market_conditions_clean
    assert 'spread' in unclean.blocking_condition


# ─────────────────────────────────────────────
# Confluence
# ─────────────────────────────────────────────

def test_all_eight_modules_are_evaluated_every_time(settings):
    """A signal may only be issued after every module has been consulted."""
    mtf = SignalPipeline(settings).mtf_engine.build(StubProvider(), 'BTCUSDT', '15m')
    result = ConfluenceEngine().evaluate(mtf)
    assert [v.module for v in result.votes] == list(MODULE_ORDER)


def test_an_opposing_higher_timeframe_is_a_hard_conflict(settings, pipeline):
    mtf = pipeline.mtf_engine.build(_conflicting_provider(), 'BTCUSDT', '15m')
    result = ConfluenceEngine().evaluate(mtf)
    assert result.direction  # a direction was still identified
    if result.has_direction:
        # Whatever side it picked, the ladder disagreement must be visible.
        assert result.timeframe_agreement < 1.0


def test_weak_agreement_blocks_the_setup(settings):
    mtf = SignalPipeline(settings).mtf_engine.build(
        StubProvider(candles=make_ranging_candles()), 'BTCUSDT', '15m'
    )
    result = ConfluenceEngine().evaluate(mtf)
    if result.has_direction and result.agreement < MIN_AGREEMENT:
        assert result.blocked


# ─────────────────────────────────────────────
# Signal generation
# ─────────────────────────────────────────────

def test_pipeline_runs_end_to_end(pipeline):
    result = pipeline.run(StubProvider(), 'BTCUSDT', '15m')

    assert result.signal.direction in (BUY, SELL, WAIT)
    assert result.mtf.symbol == 'BTCUSDT'
    assert result.mtf.selected_timeframe == '15m'
    assert result.elapsed_ms >= 0
    assert result.explanation.headline


def test_scores_are_never_null(pipeline):
    """The whole point of Phase 1: no placeholder values survive."""
    result = pipeline.run(StubProvider(), 'BTCUSDT', '15m')
    assert result.quality is not None and isinstance(result.quality.value, int)
    assert result.confidence is not None and isinstance(result.confidence.value, int)
    assert result.quality.grade and result.confidence.grade


def test_an_actionable_signal_carries_a_complete_level_set(pipeline):
    """Whenever a direction is issued, every price level must be present."""
    for provider in (
        StubProvider(candles=make_uptrend()),
        StubProvider(candles=make_downtrend()),
        StubProvider(candles=make_candles(n=500, start=100.0, end=160.0, swing_amplitude=4.0)),
    ):
        signal = pipeline.run(provider, 'BTCUSDT', '15m').signal
        if not signal.actionable:
            continue

        assert signal.entry and signal.entry > 0
        assert signal.stop_loss and signal.stop_loss > 0
        assert len(signal.take_profits) == 3
        assert signal.risk_reward and signal.risk_reward > 0
        assert signal.entry_basis and signal.stop_basis
        assert len(signal.target_sources) == 3

        if signal.direction == BUY:
            assert signal.stop_loss < signal.entry
            assert all(tp > signal.entry for tp in signal.take_profits)
        else:
            assert signal.stop_loss > signal.entry
            assert all(tp < signal.entry for tp in signal.take_profits)

        distances = [abs(tp - signal.entry) for tp in signal.take_profits]
        assert distances == sorted(distances), 'TP ladder must run away from entry'


def test_targets_come_from_named_analysis_sources(pipeline):
    """No target may be a fixed multiple — each must name the level it came from."""
    for provider in (StubProvider(candles=make_uptrend()), StubProvider(candles=make_downtrend())):
        signal = pipeline.run(provider, 'BTCUSDT', '15m').signal
        if signal.actionable:
            for source in signal.target_sources:
                assert any(word in source for word in (
                    'zone', 'Fibonacci', 'measured move',
                )), source


# ─────────────────────────────────────────────
# WAIT scenarios
# ─────────────────────────────────────────────

def test_unclean_conditions_force_wait(pipeline):
    signal = pipeline.run(StubProvider(spread=500.0), 'BTCUSDT', '15m').signal
    assert signal.direction == WAIT
    assert 'market conditions unsuitable' in signal.wait_reason


def test_conflicting_timeframes_prefer_wait(pipeline):
    """Signal quality is more important than signal quantity."""
    result = pipeline.run(_conflicting_provider(), 'BTCUSDT', '15m')
    assert result.signal.direction == WAIT
    assert result.signal.wait_reason


def test_wait_never_invents_levels(pipeline):
    for provider in (
        StubProvider(spread=500.0),
        _conflicting_provider(),
        StubProvider(candles=make_ranging_candles()),
    ):
        signal = pipeline.run(provider, 'BTCUSDT', '15m').signal
        if signal.direction == WAIT:
            assert signal.entry is None
            assert signal.stop_loss is None
            assert signal.take_profits == []
            assert signal.risk_reward is None
            assert not signal.actionable


def test_every_wait_explains_itself(pipeline):
    for provider in (
        StubProvider(spread=500.0),
        _conflicting_provider(),
        StubProvider(candles=make_ranging_candles()),
    ):
        result = pipeline.run(provider, 'BTCUSDT', '15m')
        if result.signal.direction == WAIT:
            assert result.signal.wait_reason
            assert result.explanation.headline
            assert result.explanation.against       # what is missing is stated


def test_a_signal_below_the_quality_floor_is_withheld(pipeline):
    result = pipeline.run(StubProvider(candles=make_ranging_candles()), 'BTCUSDT', '15m')
    if result.quality.value < MIN_TRADEABLE_QUALITY:
        assert result.signal.direction == WAIT


# ─────────────────────────────────────────────
# Explanations
# ─────────────────────────────────────────────

def test_reasons_are_produced_for_every_result(pipeline):
    result = pipeline.run(StubProvider(), 'BTCUSDT', '15m')
    assert result.explanation.all_reasons
    for line in result.explanation.all_reasons:
        assert line[0] in ('✓', '✕', '•')


def test_explanations_are_deterministic(pipeline):
    provider = StubProvider(candles=make_uptrend())
    first = pipeline.run(provider, 'BTCUSDT', '15m')
    second = pipeline.run(provider, 'BTCUSDT', '15m')
    assert first.explanation.all_reasons == second.explanation.all_reasons
    assert first.signal.direction == second.signal.direction


# ─────────────────────────────────────────────
# Provider independence
# ─────────────────────────────────────────────

def test_pipeline_is_provider_agnostic(pipeline):
    """The same pipeline runs against a provider from a different market."""

    class ForexLikeProvider(StubProvider):
        name = 'fx_stub'
        market = 'forex_test'

        def reference_symbol(self):
            return None          # FX has no BTC-style benchmark

    fx = ForexLikeProvider(symbols=('EURUSD', 'GBPUSD'))
    result = pipeline.run(fx, 'EURUSD', '15m')

    assert result.mtf.market == 'forex_test'
    assert result.mtf.entry.benchmark_symbol is None
    assert result.signal.direction in (BUY, SELL, WAIT)
    assert isinstance(result.quality.value, int)
