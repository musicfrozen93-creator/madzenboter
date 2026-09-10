"""Final Signal Decision Engine tests (Phase 3B).

Locks the audit contract:

  * quality < 60 → WAIT, regardless of confidence, R:R or direction;
  * confidence < 60 → WAIT, regardless of quality or R:R;
  * a large R:R never overrides quality/confidence gates;
  * probabilistic modules (Elliott) return NEUTRAL when the top vs alternative
    edge is below MIN_PROBABILITY_EDGE (default 10 pp) — a 51/49 read is a
    coin flip and must not be treated as directional evidence;
  * the final decision is expressed as one FinalDecision object with a
    machine-readable code and a UI-ready message;
  * the API response carries `tradeable`, `direction_bias`, `decision_reason`
    and `decision_message` so downstream consumers never re-derive them.
"""

from __future__ import annotations

import pytest

from analysis.decision import (
    BUY,
    CODE_CONFIDENCE_BELOW,
    CODE_HARD_CONFLICT,
    CODE_NO_DIRECTION,
    CODE_QUALITY_BELOW,
    CODE_TRADEABLE,
    FinalDecision,
    approve,
    decide,
    reject,
)
from analysis.confluence import ConfluenceResult
from analysis.modules import ELLIOTT, MODULE_ORDER, ModuleVote
from analysis.strategies import DEFAULT_STRATEGY_ID, strategy_modules
from analysis.scoring import (
    MIN_PROBABILITY_EDGE,
    MIN_TRADEABLE_CONFIDENCE,
    MIN_TRADEABLE_QUALITY,
    Score,
    ScoreComponent,
)
from analysis.structure import BEARISH, BULLISH


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _score(value: int, grade: str = 'Weak') -> Score:
    return Score(value=value, grade=grade)


def _bullish_confluence(**overrides) -> ConfluenceResult:
    base = dict(
        direction=BULLISH,
        votes=[ModuleVote('rsi', BULLISH, 0.9, 'Bullish', 'test')],
        bullish_weight=10.0, bearish_weight=0.0, agreement=1.0,
        conflicts=[], hard_conflicts=[],
        # The PRODUCTION strategy's module set. These tests exercise the
        # score, conflict and R:R gates, so the configuration must be one that
        # is allowed to reach them — gate 0 short-circuits any non-production
        # selection before the rest of the cascade runs.
        enabled_modules=strategy_modules(DEFAULT_STRATEGY_ID),
        reason='synthetic',
    )
    base.update(overrides)
    return ConfluenceResult(**base)


# ─────────────────────────────────────────────
# 1) The audit's canonical rejection cases
# ─────────────────────────────────────────────

def test_confidence_45_and_quality_59_returns_wait():
    """The exact case from the audit — ACUUSDT 30m, 59/45 with R:R 2.4."""
    decision = decide(
        market_blocking=None,
        confluence=_bullish_confluence(),
        quality=_score(59), confidence=_score(45),
    )
    assert decision.tradeable is False
    assert decision.signal == 'WAIT'
    assert decision.reason_code == CODE_QUALITY_BELOW
    assert '59/100' in decision.reason_message
    assert '60/100' in decision.reason_message


def test_confidence_59_and_quality_80_returns_wait():
    decision = decide(
        market_blocking=None,
        confluence=_bullish_confluence(),
        quality=_score(80, 'Good'), confidence=_score(59, 'Moderate'),
    )
    assert decision.tradeable is False
    assert decision.reason_code == CODE_CONFIDENCE_BELOW


def test_quality_90_confidence_59_returns_wait():
    decision = decide(
        market_blocking=None,
        confluence=_bullish_confluence(),
        quality=_score(90, 'High Quality'), confidence=_score(59, 'Moderate'),
    )
    assert decision.tradeable is False
    assert decision.reason_code == CODE_CONFIDENCE_BELOW


def test_quality_59_confidence_90_returns_wait():
    decision = decide(
        market_blocking=None,
        confluence=_bullish_confluence(),
        quality=_score(59), confidence=_score(90, 'Very High'),
    )
    assert decision.tradeable is False
    assert decision.reason_code == CODE_QUALITY_BELOW


def test_rr_cannot_compensate_for_low_confidence():
    """R:R never even enters the pre-risk decide() call — a 5:1 R:R does
    nothing when confidence is 40."""
    decision = decide(
        market_blocking=None,
        confluence=_bullish_confluence(),
        quality=_score(70, 'Average'), confidence=_score(40, 'Low'),
    )
    assert decision.tradeable is False
    assert decision.reason_code == CODE_CONFIDENCE_BELOW


def test_both_at_threshold_is_tradeable():
    """60/60 exactly clears the floors — the pre-risk decision is BUY-eligible."""
    decision = decide(
        market_blocking=None,
        confluence=_bullish_confluence(),
        quality=_score(60, 'Weak'), confidence=_score(60, 'Moderate'),
    )
    assert decision.tradeable is True
    assert decision.signal == BUY
    assert decision.reason_code == CODE_TRADEABLE


# ─────────────────────────────────────────────
# 2) Thresholds are the authoritative floors
# ─────────────────────────────────────────────

def test_thresholds_lifted_from_35_to_60():
    """The old permissive floors are gone — Phase 3B raised both to 60."""
    assert MIN_TRADEABLE_QUALITY == 60
    assert MIN_TRADEABLE_CONFIDENCE == 60


def test_thresholds_are_overridable_per_call():
    """A stricter caller can raise the floor without touching module constants."""
    decision = decide(
        market_blocking=None,
        confluence=_bullish_confluence(),
        quality=_score(70), confidence=_score(70, 'High'),
        min_quality=75, min_confidence=60,
    )
    assert decision.tradeable is False
    assert decision.reason_code == CODE_QUALITY_BELOW


# ─────────────────────────────────────────────
# 3) Direction / conflict handling
# ─────────────────────────────────────────────

def test_no_directional_consensus_returns_wait():
    decision = decide(
        market_blocking=None,
        confluence=_bullish_confluence(direction='range', reason='synthetic range'),
        quality=_score(80, 'Good'), confidence=_score(80, 'High'),
    )
    assert decision.tradeable is False
    assert decision.reason_code == CODE_NO_DIRECTION


def test_hard_conflict_returns_wait_with_message():
    decision = decide(
        market_blocking=None,
        confluence=_bullish_confluence(
            hard_conflicts=['higher timeframe (4h) is bearish, against a bullish setup'],
        ),
        quality=_score(80, 'Good'), confidence=_score(80, 'High'),
    )
    assert decision.tradeable is False
    assert decision.reason_code == CODE_HARD_CONFLICT
    assert 'higher timeframe' in decision.reason_message


def test_market_unclean_wins_over_score():
    """Unclean conditions are a hard fail regardless of great scores."""
    decision = decide(
        market_blocking='spread: spread 5.0000% vs ceiling 0.1000%',
        confluence=_bullish_confluence(),
        quality=_score(95, 'Elite Setup'), confidence=_score(95, 'Very High'),
    )
    assert decision.tradeable is False
    assert 'Market conditions unsuitable' in decision.reason_message


# ─────────────────────────────────────────────
# 4) Probabilistic uncertainty: Elliott 51/49 → neutral
# ─────────────────────────────────────────────

def test_min_probability_edge_is_configurable_default_10pp():
    assert MIN_PROBABILITY_EDGE == 10.0


def test_elliott_5149_is_treated_as_neutral():
    """A 51 vs 49 read is a coin flip and must not vote directionally."""
    from analysis.elliott import ElliottState, WaveCount
    from analysis.modules import _elliott_vote

    class _Picture:
        def __init__(self, elliott: ElliottState) -> None:
            self.elliott = elliott

    primary = WaveCount(
        label='Wave C', degree='corrective', direction=BEARISH,
        confidence=51.0, completion_pct=40.0, projected_target=None,
        wave_start=100.0, pivots_used=3,
    )
    alt = WaveCount(
        label='Wave 3', degree='impulse', direction=BULLISH,
        confidence=49.0, completion_pct=30.0, projected_target=None,
        wave_start=100.0, pivots_used=3,
    )
    state = ElliottState(primary=primary, alternative=alt, direction=BEARISH,
                        confidence=51.0, pivots_available=6, reason='synthetic')

    vote = _elliott_vote(_Picture(state))
    assert vote.direction == 'neutral'
    assert vote.strength == 0.0
    assert 'Uncertain' in vote.label
    assert 'below the 10' in vote.detail


def test_elliott_7030_is_meaningful_directional_evidence():
    from analysis.elliott import ElliottState, WaveCount
    from analysis.modules import _elliott_vote

    class _Picture:
        def __init__(self, elliott: ElliottState) -> None:
            self.elliott = elliott

    primary = WaveCount(
        label='Wave 3', degree='impulse', direction=BULLISH,
        confidence=70.0, completion_pct=60.0, projected_target=None,
        wave_start=100.0, pivots_used=5,
    )
    alt = WaveCount(
        label='Wave B', degree='corrective', direction=BEARISH,
        confidence=30.0, completion_pct=40.0, projected_target=None,
        wave_start=100.0, pivots_used=3,
    )
    state = ElliottState(primary=primary, alternative=alt, direction=BULLISH,
                        confidence=70.0, pivots_available=6, reason='synthetic')

    vote = _elliott_vote(_Picture(state))
    assert vote.direction == BULLISH
    assert vote.strength > 0.5


# ─────────────────────────────────────────────
# 5) FinalDecision helpers
# ─────────────────────────────────────────────

def test_approve_produces_tradeable_signal():
    d = approve('long')
    assert d.tradeable is True
    assert d.signal == 'BUY'
    assert d.reason_code == CODE_TRADEABLE


def test_reject_produces_wait_with_code():
    d = reject('long', 'no_valid_stop', 'No valid stop level — ATR unavailable.')
    assert d.tradeable is False
    assert d.signal == 'WAIT'
    assert d.direction_bias == 'long'
    assert d.reason_code == 'no_valid_stop'


# ─────────────────────────────────────────────
# 6) The generator honours the central decision layer end-to-end
# ─────────────────────────────────────────────

@pytest.fixture
def _pipeline_result(settings):
    from analysis.cache import CANDLE_CACHE
    from analysis.pipeline import SignalPipeline
    from tests.fakes import StubProvider, make_uptrend
    CANDLE_CACHE.clear()
    provider = StubProvider(candles=make_uptrend(n=500))
    return SignalPipeline(settings).run(provider, 'BTCUSDT', '15m')


def test_generator_populates_decision_fields_on_wait(settings):
    """Force a WAIT via a low-confidence synthetic score and verify the
    signal carries the new authoritative fields."""
    from analysis.generator import SignalGenerator
    from analysis.timeframes import MultiTimeframeEngine, TREND, ENTRY
    from analysis.engine import AnalysisEngine
    from analysis.cache import CANDLE_CACHE
    from tests.fakes import StubProvider, make_uptrend
    CANDLE_CACHE.clear()
    mtf = MultiTimeframeEngine(settings, AnalysisEngine(settings)).build(
        StubProvider(candles=make_uptrend()), 'BTCUSDT', '15m',
    )

    generator = SignalGenerator()
    quality = _score(45, 'Weak')
    confidence = _score(30, 'Very Low')
    signal = generator.generate(mtf, _bullish_confluence(), quality, confidence)

    assert signal.direction == 'WAIT'
    assert signal.tradeable is False
    assert signal.decision_code == CODE_QUALITY_BELOW
    assert signal.decision_message
    assert signal.direction_bias == 'long'


def test_generator_marks_tradeable_when_all_gates_pass(_pipeline_result):
    """A real pipeline run either emits WAIT with a code, or BUY/SELL with
    tradeable=true and decision_code=tradeable — never a mixed state."""
    sig = _pipeline_result.signal
    if sig.direction == 'WAIT':
        assert sig.tradeable is False
        assert sig.decision_code and sig.decision_code != CODE_TRADEABLE
        assert sig.decision_message
    else:
        assert sig.tradeable is True
        assert sig.decision_code == CODE_TRADEABLE
        assert sig.entry is not None
        assert sig.stop_loss is not None


# ─────────────────────────────────────────────
# 7) The API serializer exposes the fields
# ─────────────────────────────────────────────

def test_analyze_response_carries_tradeable_and_decision(_pipeline_result):
    from api.serializers import to_analyze_response
    resp = to_analyze_response(_pipeline_result)
    assert hasattr(resp, 'tradeable')
    assert hasattr(resp, 'direction_bias')
    assert hasattr(resp, 'decision_reason')
    assert hasattr(resp, 'decision_message')
    if resp.signal == 'WAIT':
        assert resp.tradeable is False
        assert resp.decision_reason
        assert resp.decision_message
    else:
        assert resp.tradeable is True
        assert resp.decision_reason == CODE_TRADEABLE
