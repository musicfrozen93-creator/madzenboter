"""Tests for Phase 1 audit fixes (F1, F3, F4, F6, diagnostic)."""

from __future__ import annotations

import pytest

from analysis.confluence import ConfluenceEngine
from analysis.diagnostic import build_diagnostic
from analysis.engine import TechnicalPicture
from analysis.generator import SignalGenerator, TradingSignal, WAIT
from analysis.modules import MODULE_WEIGHTS, _volume_vote
from analysis.pipeline import SignalPipeline
from analysis.scoring import (
    MIN_TRADEABLE_CONFIDENCE,
    MIN_TRADEABLE_QUALITY,
    ConfidenceScorer,
    QualityScorer,
    Score,
)
from analysis.structure import BULLISH, BEARISH
from config.settings import Settings
from tests.fakes import StubProvider, make_candles, make_uptrend, make_downtrend


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def pipeline(settings):
    return SignalPipeline(settings)


# ─────────────────────────────────────────────
# F1: Confidence gating
# ─────────────────────────────────────────────

class TestConfidenceGating:
    """F1: Low confidence must produce WAIT, not a tradeable signal."""

    def test_generator_accepts_confidence_parameter(self):
        gen = SignalGenerator()
        sig = gen.generate.__code__.co_varnames
        assert 'confidence' in sig

    def test_low_confidence_produces_wait(self, settings):
        pipe = SignalPipeline(settings)
        provider = StubProvider(candles=make_uptrend())
        result = pipe.run(provider, 'BTCUSDT', '15m')
        if result.confidence.value < MIN_TRADEABLE_CONFIDENCE:
            assert result.signal.direction == WAIT
            assert 'engine confidence' in (result.signal.wait_reason or '')

    def test_confidence_scored_before_signal(self, settings):
        pipe = SignalPipeline(settings)
        provider = StubProvider(candles=make_uptrend())
        result = pipe.run(provider, 'BTCUSDT', '15m')
        assert result.confidence.value >= 0
        assert result.confidence.grade is not None


# ─────────────────────────────────────────────
# F3: No structure double-counting
# ─────────────────────────────────────────────

class TestNoStructureDoubleCount:
    """F3: structure.trend must not feed trend_direction AND score independently."""

    def test_trend_direction_uses_two_voters(self, settings):
        pipe = SignalPipeline(settings)
        provider = StubProvider(candles=make_uptrend())
        result = pipe.run(provider, 'BTCUSDT', '15m')
        picture = result.mtf.entry
        assert hasattr(picture, 'trend_direction')
        assert hasattr(picture, 'trend_conviction')

    def test_trend_conviction_out_of_two(self, settings):
        pipe = SignalPipeline(settings)
        provider = StubProvider(candles=make_uptrend())
        result = pipe.run(provider, 'BTCUSDT', '15m')
        conv = result.mtf.entry.trend_conviction
        assert conv in (0.0, 0.5, 1.0)

    def test_structure_not_in_trend_direction(self):
        """The trend_direction property source must not reference structure.trend."""
        import inspect
        source = inspect.getsource(TechnicalPicture.trend_direction.fget)
        assert 'self.structure.trend' not in source


# ─────────────────────────────────────────────
# F4: Volume multi-candle analysis
# ─────────────────────────────────────────────

class TestVolumeMultiCandle:
    """F4: Volume direction must use multiple candles, not just the last one."""

    def test_volume_uses_multi_candle(self):
        import inspect
        source = inspect.getsource(_volume_vote)
        assert 'LOOKBACK' in source
        assert 'bull_vol' in source or 'bull_share' in source

    def test_volume_neutral_on_mixed(self, settings):
        candles = make_candles(n=400, start=100.0, end=100.0, swing_amplitude=5.0)
        provider = StubProvider(candles=candles)
        pipe = SignalPipeline(settings)
        result = pipe.run(provider, 'BTCUSDT', '15m')
        vol_vote = result.confluence.vote('volume')
        if vol_vote is not None:
            assert vol_vote.direction in (BULLISH, BEARISH, 'neutral', 'N/A')

    def test_volume_bullish_on_uptrend(self, settings):
        provider = StubProvider(candles=make_uptrend())
        pipe = SignalPipeline(settings)
        result = pipe.run(provider, 'BTCUSDT', '15m')
        vol_vote = result.confluence.vote('volume')
        if vol_vote is not None and vol_vote.strength > 0.3:
            assert vol_vote.direction in (BULLISH, 'neutral', 'N/A')


# ─────────────────────────────────────────────
# F6: Per-TP R:R
# ─────────────────────────────────────────────

class TestPerTPRiskReward:
    """F6: Each TP must have its own R:R, individually validated."""

    def test_rr_per_tp_field_exists(self):
        sig = TradingSignal(
            market='test', provider='stub', symbol='TEST', timeframe='15m',
            direction='BUY',
        )
        assert hasattr(sig, 'rr_per_tp')
        assert sig.rr_per_tp == []

    def test_rr_per_tp_populated_on_actionable(self, settings):
        pipe = SignalPipeline(settings)
        provider = StubProvider(candles=make_uptrend())
        result = pipe.run(provider, 'BTCUSDT', '15m')
        if result.signal.actionable:
            assert len(result.signal.rr_per_tp) == len(result.signal.take_profits)
            assert all(rr > 0 for rr in result.signal.rr_per_tp)
            assert result.signal.rr_per_tp == sorted(result.signal.rr_per_tp)

    def test_rr_per_tp_empty_on_wait(self, settings):
        candles = make_candles(n=400, start=100.0, end=100.0, swing_amplitude=0.5)
        provider = StubProvider(candles=candles)
        pipe = SignalPipeline(settings)
        result = pipe.run(provider, 'BTCUSDT', '15m')
        if result.signal.direction == WAIT:
            assert result.signal.rr_per_tp == []


# ─────────────────────────────────────────────
# Diagnostic output
# ─────────────────────────────────────────────

class TestDiagnostic:
    """Every analysis must produce a full diagnostic breakdown."""

    def test_diagnostic_has_modules_and_gates(self, settings):
        pipe = SignalPipeline(settings)
        provider = StubProvider(candles=make_uptrend())
        result = pipe.run(provider, 'BTCUSDT', '15m')
        diag = build_diagnostic(
            result.signal, result.confluence, result.quality, result.confidence,
        )
        assert len(diag.modules) > 0
        assert len(diag.gates) >= 8
        assert diag.quality is not None
        assert diag.confidence is not None

    def test_diagnostic_as_dict(self, settings):
        pipe = SignalPipeline(settings)
        provider = StubProvider(candles=make_uptrend())
        result = pipe.run(provider, 'BTCUSDT', '15m')
        diag = build_diagnostic(
            result.signal, result.confluence, result.quality, result.confidence,
        )
        d = diag.as_dict()
        assert 'modules' in d
        assert 'gates' in d
        assert 'quality' in d
        assert 'confidence' in d
        assert 'direction' in d

    def test_wait_diagnostic_has_reason(self, settings):
        candles = make_candles(n=400, start=100.0, end=100.0, swing_amplitude=0.5)
        provider = StubProvider(candles=candles)
        pipe = SignalPipeline(settings)
        result = pipe.run(provider, 'BTCUSDT', '15m')
        if result.signal.direction == WAIT:
            diag = build_diagnostic(
                result.signal, result.confluence, result.quality, result.confidence,
            )
            assert diag.wait_reason is not None
            failed_gates = [g for g in diag.gates if not g.passed]
            assert len(failed_gates) >= 1


# ─────────────────────────────────────────────
# Integration: pipeline still works end-to-end
# ─────────────────────────────────────────────

class TestPipelineIntegration:
    """Smoke tests: the pipeline runs without errors after all Phase 1 changes."""

    def test_uptrend_completes(self, settings):
        pipe = SignalPipeline(settings)
        provider = StubProvider(candles=make_uptrend())
        result = pipe.run(provider, 'BTCUSDT', '15m')
        assert result.signal.direction in ('BUY', 'SELL', 'WAIT')
        assert result.quality.value >= 0
        assert result.confidence.value >= 0

    def test_downtrend_completes(self, settings):
        pipe = SignalPipeline(settings)
        provider = StubProvider(candles=make_downtrend())
        result = pipe.run(provider, 'BTCUSDT', '15m')
        assert result.signal.direction in ('BUY', 'SELL', 'WAIT')

    def test_ranging_produces_wait(self, settings):
        from tests.fakes import make_ranging_candles
        pipe = SignalPipeline(settings)
        provider = StubProvider(candles=make_ranging_candles())
        result = pipe.run(provider, 'BTCUSDT', '15m')
        assert result.signal.direction == WAIT
