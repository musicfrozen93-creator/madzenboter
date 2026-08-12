"""Configurable-module scoring — the "disabled = neutral, never negative" contract.

These tests pin the exact behaviour required when a user turns optional analysis
modules off:

  * a disabled module contributes ZERO and is removed from the score's maximum
    (it is excluded, not penalised);
  * disabling a module that OPPOSES the setup never lowers the score — it can
    only raise it, proving "disabled" is treated as excluded rather than as
    negative evidence;
  * required modules can never be disabled;
  * unknown/misspelled indicator names are ignored (no arbitrary internals);
  * the tradeable threshold stays reachable with a reduced module set.

QualityScorer ignores `mtf` entirely (it scores off the confluence votes), so
most cases are exercised with a synthetic ConfluenceResult for precise control.
Confidence exclusion is checked through the real engine.
"""

import pytest

from analysis.cache import CANDLE_CACHE
from analysis.confluence import ConfluenceEngine, ConfluenceResult
from analysis.engine import AnalysisEngine
from analysis.modules import (
    MODULE_ORDER,
    MODULE_WEIGHTS,
    OPTIONAL_MODULES,
    REQUIRED_MODULES,
    ModuleVote,
    resolve_enabled_modules,
)
from analysis.scoring import ConfidenceScorer, QualityScorer
from analysis.structure import BEARISH, BULLISH
from analysis.timeframes import MultiTimeframeEngine
from tests.fakes import StubProvider, make_uptrend


# ── Helpers ──────────────────────────────────────────────

def _vote(module, direction=BULLISH, strength=1.0):
    return ModuleVote(module, direction, strength, module, 'detail')


def _confluence(votes, direction=BULLISH, enabled=None):
    """Build a ConfluenceResult over the given votes, filtered to `enabled`."""
    active_set = frozenset(MODULE_ORDER) if enabled is None else frozenset(enabled)
    active = [v for v in votes if v.module in active_set]
    bull = sum(v.weighted_strength for v in active if v.direction == BULLISH)
    bear = sum(v.weighted_strength for v in active if v.direction == BEARISH)
    return ConfluenceResult(
        direction=direction, votes=active,
        bullish_weight=bull, bearish_weight=bear,
        agreement=1.0, enabled_modules=active_set,
    )


def _all_bullish_votes():
    return [_vote(m, BULLISH, 1.0) for m in MODULE_ORDER]


# ── resolve_enabled_modules ───────────────────────────────

def test_none_selects_every_module():
    assert resolve_enabled_modules(None) == frozenset(MODULE_ORDER)


def test_required_modules_are_always_forced_on():
    # User asks for only RSI; trend & structure (required) come along regardless.
    resolved = resolve_enabled_modules(['rsi'])
    assert REQUIRED_MODULES <= resolved
    assert 'rsi' in resolved
    assert 'volume' not in resolved


def test_unknown_indicator_names_are_ignored():
    # CASE H — a client cannot name arbitrary internals or invent indicators.
    resolved = resolve_enabled_modules(['rsi', 'totally_fake', 'DROP TABLE'])
    assert resolved == REQUIRED_MODULES | {'rsi'}


def test_required_modules_cannot_be_disabled_even_if_omitted():
    resolved = resolve_enabled_modules(['macd'])  # trend/structure omitted
    assert REQUIRED_MODULES <= resolved


# ── Quality: disabled = excluded, never negative ──────────

def test_case_a_all_enabled_all_bullish_is_full_marks():
    """CASE A — every optional enabled, all evidence bullish → 100."""
    quality = QualityScorer().score(None, _confluence(_all_bullish_votes()))
    assert quality.value == 100


def test_disabled_module_is_absent_from_the_breakdown():
    enabled = frozenset(MODULE_ORDER) - {'volume'}
    quality = QualityScorer().score(None, _confluence(_all_bullish_votes(), enabled=enabled))
    names = [c.name for c in quality.components]
    assert 'volume' not in names
    assert len(names) == len(MODULE_ORDER) - 1


def test_disabling_an_opposing_module_never_lowers_the_score():
    """CASE E/F core rule — DISABLED must be neutral/excluded, not negative.

    Make one optional module oppose the long setup. Disabling it removes an
    honest drag on the score, so the disabled score is >= the enabled score.
    A disabled module can therefore never reduce the score the way a present,
    opposing one does.
    """
    votes = _all_bullish_votes()
    # Flip an optional module (volume, weight 8) to firmly oppose the long side.
    votes = [_vote('volume', BEARISH, 1.0) if v.module == 'volume' else v for v in votes]

    enabled_score = QualityScorer().score(None, _confluence(votes)).value
    disabled_score = QualityScorer().score(
        None, _confluence(votes, enabled=frozenset(MODULE_ORDER) - {'volume'})
    ).value

    assert disabled_score >= enabled_score
    # Concretely: with volume opposing it drags 8pts off the max→92; excluding it
    # renormalises the remaining full-credit evidence back to 100.
    assert enabled_score == 92
    assert disabled_score == 100


def test_disabled_module_is_not_counted_as_a_conflict():
    """A user-disabled module must not appear as opposing evidence anywhere."""
    votes = [_vote('volume', BEARISH, 1.0) if v.module == 'volume' else v
             for v in _all_bullish_votes()]
    conf = _confluence(votes, enabled=frozenset(MODULE_ORDER) - {'volume'})
    assert all(v.module != 'volume' for v in conf.votes)


def test_case_d_minimum_config_can_still_reach_the_tradeable_threshold():
    """CASE D — required modules + one optional, all bullish → still tradeable.

    Disabling optionals must NOT make the score mathematically unreachable.
    """
    enabled = REQUIRED_MODULES | {'rsi'}
    quality = QualityScorer().score(None, _confluence(_all_bullish_votes(), enabled=enabled))
    assert quality.value >= 50           # MIN_TRADEABLE_QUALITY
    assert quality.value == 100          # all enabled evidence agrees → full marks


def test_case_g_conflicting_evidence_still_scores_lower():
    """CASE G — disabling modules does not let a conflicted setup score full marks."""
    # Required bullish, but several enabled optionals bearish.
    votes = []
    for m in MODULE_ORDER:
        if m in REQUIRED_MODULES:
            votes.append(_vote(m, BULLISH, 1.0))
        elif m in ('rsi', 'macd', 'fibonacci'):
            votes.append(_vote(m, BEARISH, 1.0))     # genuine opposition
        else:
            votes.append(_vote(m, BULLISH, 1.0))
    conflicted = QualityScorer().score(None, _confluence(votes)).value
    clean = QualityScorer().score(None, _confluence(_all_bullish_votes())).value
    assert conflicted < clean


def test_strength_modules_still_earn_credit_when_enabled():
    """ATR/ADX are directionless — enabled, they contribute their suitability."""
    quality = QualityScorer().score(None, _confluence(_all_bullish_votes()))
    by_name = {c.name: c for c in quality.components}
    assert by_name['atr'].points > 0
    assert by_name['adx'].points > 0


# ── Confidence: disabled component dropped & renormalised ──

@pytest.fixture
def _mtf(settings):
    CANDLE_CACHE.clear()
    return MultiTimeframeEngine(settings, AnalysisEngine(settings)).build(
        StubProvider(candles=make_uptrend()), 'BTCUSDT', '15m'
    )


def test_disabling_a_module_drops_its_confidence_component(_mtf):
    enabled = frozenset(MODULE_ORDER) - {'volume'}
    conf = ConfluenceEngine().evaluate(_mtf, enabled_modules=enabled)
    confidence = ConfidenceScorer().score(_mtf, conf)
    names = {c.name for c in confidence.components}
    assert 'volume_confirmation' not in names
    assert isinstance(confidence.value, int)
    assert 0 <= confidence.value <= 100


def test_default_confidence_still_has_every_component(_mtf):
    conf = ConfluenceEngine().evaluate(_mtf)          # all enabled
    confidence = ConfidenceScorer().score(_mtf, conf)
    assert 'volume_confirmation' in {c.name for c in confidence.components}
