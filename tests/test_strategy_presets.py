"""Strategy presets — the ICT + MSNR default and what it actually analyses.

Yaksha AI's primary methodology is ICT + MSNR, and a NEW analysis must start
there without the user configuring anything. These tests pin:

  * the default preset and its EXACT composition (which modules are on, which
    are deliberately off);
  * that a preset is a real backend configuration, not a cosmetic label —
    turning it on changes what the engine scores;
  * that ICT/MSNR evidence is counted ONCE, even though the same fair value
    gap / order block / liquidity pool is visible to several layers;
  * that every pre-existing gate (quality, confidence, R:R, probabilistic
    uncertainty) is untouched by the new default.

The dedup tests are the important ones: ICT_FVG and FVG describe the same gap,
so the engine must never let that gap score twice.
"""

import pytest

from analysis.confluence import ConfluenceEngine, ConfluenceResult
from analysis.decision import (
    CODE_CONFIDENCE_BELOW,
    CODE_QUALITY_BELOW,
    CODE_TRADEABLE,
    WAIT,
    decide,
)
from analysis.ict.evidence import EvidenceItem, EvidenceRegistry, make_evidence_id
from analysis.modules import (
    ADX,
    ALL_MODULE_KEYS,
    ATR,
    CONFIRMATION_MODULES,
    CONTEXTUAL_MODULES,
    DEFAULT_PRESET,
    ELLIOTT,
    FIBONACCI,
    FVG,
    ICT_FVG,
    ICT_LIQUIDITY,
    ICT_MODULES,
    ICT_MSNR_CARRIERS,
    ICT_ORDER_BLOCKS,
    LIQUIDITY,
    MACD,
    MODULE_ORDER,
    MODULE_WEIGHTS,
    MSNR_MODULES,
    OPTIONAL_UI_KEYS,
    ORDER_BLOCK,
    PATTERN,
    PRESET_BALANCED,
    PRESET_CONSERVATIVE,
    PRESET_CUSTOM,
    PRESET_ICT_MSNR,
    PRESETS,
    UnknownPresetError,
    REQUIRED_MODULES,
    RSI,
    STRUCTURE,
    SUPPORT_RESISTANCE,
    TREND,
    VOLUME,
    VWAP,
    ModuleVote,
    describe_presets,
    identify_preset,
    preset_modules,
    resolve_enabled_modules,
)
from analysis.scoring import (
    MIN_PROBABILITY_EDGE,
    MIN_TRADEABLE_CONFIDENCE,
    MIN_TRADEABLE_QUALITY,
    QualityScorer,
    Score,
    grade_for,
)
from analysis.structure import BEARISH, BULLISH


# ── Helpers ──────────────────────────────────────────────

def _vote(module, direction=BULLISH, strength=1.0):
    return ModuleVote(module, direction, strength, module, 'detail')


def _confluence(enabled, votes=None, direction=BULLISH):
    """A ConfluenceResult restricted to `enabled`, all-bullish unless told otherwise."""
    active = frozenset(enabled)
    all_votes = votes if votes is not None else [_vote(m) for m in MODULE_ORDER]
    kept = [v for v in all_votes if v.module in active]
    bull = sum(v.weighted_strength for v in kept if v.direction == BULLISH)
    bear = sum(v.weighted_strength for v in kept if v.direction == BEARISH)
    return ConfluenceResult(
        direction=direction, votes=kept,
        bullish_weight=bull, bearish_weight=bear,
        agreement=1.0, enabled_modules=active,
    )


def _weighted(active):
    """The weighted-vote modules inside an enabled set."""
    return [m for m in MODULE_ORDER if m in active]


def _weight_of(active):
    return sum(MODULE_WEIGHTS[m] for m in _weighted(active))


DEFAULT_ACTIVE = preset_modules(DEFAULT_PRESET)


# ─────────────────────────────────────────────────────────
# 1. A new analysis defaults to ICT + MSNR
# ─────────────────────────────────────────────────────────

def test_default_preset_is_ict_msnr():
    assert DEFAULT_PRESET == PRESET_ICT_MSNR
    assert identify_preset(DEFAULT_ACTIVE) == PRESET_ICT_MSNR


def test_default_preset_is_flagged_default_in_the_registry():
    defaults = [p for p in describe_presets() if p['default']]
    assert [p['id'] for p in defaults] == [PRESET_ICT_MSNR]


def test_registry_publishes_every_preset_with_real_module_keys():
    for preset in describe_presets():
        assert preset['modules'], f'{preset["id"]} has no modules'
        assert set(preset['modules']) <= OPTIONAL_UI_KEYS
        assert preset['label'] and preset['description']


# ─────────────────────────────────────────────────────────
# 2. Core modules are always enabled
# ─────────────────────────────────────────────────────────

def test_core_modules_are_on_in_every_preset():
    for preset_id in PRESETS:
        assert REQUIRED_MODULES <= preset_modules(preset_id), preset_id


def test_core_modules_cannot_be_switched_off_in_custom_mode():
    # A user unticking everything still analyses trend + structure.
    assert resolve_enabled_modules([]) == REQUIRED_MODULES
    assert {TREND, STRUCTURE} <= resolve_enabled_modules(['rsi'])


def test_core_is_never_listed_as_an_optional_preset_key():
    # Presets name OPTIONAL keys only — core is implicit, never a checkbox.
    for preset_id, keys in PRESETS.items():
        assert not (keys & REQUIRED_MODULES), preset_id


# ─────────────────────────────────────────────────────────
# 3. The six deliberately-excluded modules are off by default
# ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('module', [ELLIOTT, FIBONACCI, RSI, MACD, ADX, PATTERN])
def test_module_is_disabled_under_the_default_preset(module):
    assert module not in DEFAULT_ACTIVE


def test_default_preset_disables_exactly_those_six_and_no_others():
    optional_weighted = frozenset(MODULE_ORDER) - REQUIRED_MODULES
    assert optional_weighted - DEFAULT_ACTIVE == frozenset(
        {ELLIOTT, FIBONACCI, RSI, MACD, ADX, PATTERN}
    )


# ─────────────────────────────────────────────────────────
# 4. Confirmation layer is on
# ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('module', [VOLUME, ATR, VWAP])
def test_confirmation_module_is_enabled_by_default(module):
    assert module in DEFAULT_ACTIVE


def test_confirmation_layer_is_exactly_volume_atr_vwap():
    assert set(CONFIRMATION_MODULES) == {VOLUME, ATR, VWAP}


def test_default_preset_enables_every_ict_and_msnr_toggle():
    assert frozenset(ICT_MODULES) <= DEFAULT_ACTIVE
    assert frozenset(MSNR_MODULES) <= DEFAULT_ACTIVE


def test_default_preset_enables_the_weighted_evidence_carriers():
    # The ICT/MSNR toggles are contextual (weight 0). Without these four the
    # primary strategy would carry no scoring weight at all.
    assert frozenset(ICT_MSNR_CARRIERS) <= DEFAULT_ACTIVE
    assert set(ICT_MSNR_CARRIERS) == {ORDER_BLOCK, FVG, LIQUIDITY, SUPPORT_RESISTANCE}


def test_default_preset_carries_a_substantial_scoring_weight():
    # trend 15 + structure 15 + s/r 8 + order_block 8 + fvg 5 + liquidity 5
    # + volume 8 + atr 4 + vwap 3 = 71
    assert _weight_of(DEFAULT_ACTIVE) == 71


# ─────────────────────────────────────────────────────────
# 5. A disabled module contributes nothing at all
# ─────────────────────────────────────────────────────────

def test_disabled_module_earns_no_points_and_leaves_the_denominator():
    active = DEFAULT_ACTIVE
    quality = QualityScorer().score(None, _confluence(active))
    scored = {c.name for c in quality.components}
    assert scored == set(_weighted(active))
    for off in (ELLIOTT, RSI, MACD, ADX, PATTERN, FIBONACCI):
        assert off not in scored
    # Everything enabled and bullish → full marks against the REDUCED maximum.
    assert quality.value == 100


def test_disabled_module_cannot_be_counted_against_the_setup():
    # Same votes, but the six excluded modules all oppose. Under the default
    # preset they are absent, so the score must be identical to all-bullish.
    votes = [_vote(m) for m in MODULE_ORDER]
    opposed = [
        _vote(m, BEARISH, 1.0) if m in {ELLIOTT, FIBONACCI, RSI, MACD, ADX, PATTERN}
        else _vote(m)
        for m in MODULE_ORDER
    ]
    clean = QualityScorer().score(None, _confluence(DEFAULT_ACTIVE, votes))
    dirty = QualityScorer().score(None, _confluence(DEFAULT_ACTIVE, opposed))
    assert clean.value == dirty.value == 100


def test_disabled_module_casts_no_vote_and_raises_no_conflict():
    conf = _confluence(DEFAULT_ACTIVE)
    voted = {v.module for v in conf.votes}
    assert ELLIOTT not in voted and RSI not in voted
    assert conf.vote(ELLIOTT) is None
    assert all('elliott' not in c for c in conf.conflicts)


def test_contextual_toggles_add_no_scoring_surface():
    # Enabling all 16 ICT/MSNR toggles must not change the quality denominator.
    weighted_only = frozenset(_weighted(DEFAULT_ACTIVE))
    assert _weight_of(weighted_only) == _weight_of(DEFAULT_ACTIVE)
    for key in CONTEXTUAL_MODULES:
        assert MODULE_WEIGHTS.get(key, 0) == 0


# ─────────────────────────────────────────────────────────
# 6–8. ICT + MSNR must not double-count shared evidence
# ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    'ict_key,carrier',
    [
        (ICT_FVG, FVG),
        (ICT_ORDER_BLOCKS, ORDER_BLOCK),
        (ICT_LIQUIDITY, LIQUIDITY),
    ],
)
def test_ict_toggle_adds_no_second_weighted_vote(ict_key, carrier):
    """The ICT view of a gap/block/pool is the SAME event as its carrier.

    Both are enabled under ICT + MSNR, so this is the exact configuration where
    double-counting would show up: the ICT key must carry zero weight, leaving
    the carrier as the one and only scoring path for that evidence.
    """
    assert ict_key in DEFAULT_ACTIVE and carrier in DEFAULT_ACTIVE
    assert MODULE_WEIGHTS.get(ict_key, 0) == 0
    assert MODULE_WEIGHTS[carrier] > 0

    with_ict = _weight_of(DEFAULT_ACTIVE)
    without_ict = _weight_of(DEFAULT_ACTIVE - {ict_key})
    assert with_ict == without_ict

    # And it casts no vote either, so confluence sees one opinion, not two.
    conf = _confluence(DEFAULT_ACTIVE)
    assert len([v for v in conf.votes if v.module in (ict_key, carrier)]) == 1


@pytest.mark.parametrize('kind', ['fvg', 'order_block', 'liquidity_sweep'])
def test_the_same_event_seen_twice_is_stored_once(kind):
    """Two layers detecting one event at one price collapse to a single item."""
    registry = EvidenceRegistry()
    price = 42000.0
    registry.add(EvidenceItem(
        make_evidence_id(kind, price), kind, BULLISH, price, 0.6,
        'smart_money', 'detected by the classical module',
    ))
    registry.add(EvidenceItem(
        make_evidence_id(kind, price), kind, BULLISH, price, 0.9,
        'ict', 'the same event, seen by the ICT layer',
    ))
    assert len(registry.items) == 1
    assert registry.dedup_count == 1
    # The stronger reading survives; the weaker one is recorded, not scored.
    assert registry.items[0].strength == 0.9
    assert len(registry.bullish()) == 1


def test_distinct_events_are_not_merged():
    registry = EvidenceRegistry()
    registry.add(EvidenceItem(make_evidence_id('fvg', 42000.0), 'fvg', BULLISH, 42000.0, 0.6, 'a', ''))
    registry.add(EvidenceItem(make_evidence_id('fvg', 43000.0), 'fvg', BULLISH, 43000.0, 0.6, 'b', ''))
    assert len(registry.items) == 2
    assert registry.dedup_count == 0


def test_quality_is_identical_with_and_without_the_contextual_layer():
    """Turning the whole ICT/MSNR toggle layer off cannot change the score.

    If the toggles were secretly scoring, removing them would move the number.
    """
    carriers_only = frozenset(_weighted(DEFAULT_ACTIVE))
    full = QualityScorer().score(None, _confluence(DEFAULT_ACTIVE))
    stripped = QualityScorer().score(None, _confluence(carriers_only))
    assert full.value == stripped.value
    assert len(full.components) == len(stripped.components)


# ─────────────────────────────────────────────────────────
# 9. Custom mode
# ─────────────────────────────────────────────────────────

def test_custom_selection_is_honoured_verbatim():
    chosen = [RSI, MACD, VOLUME]
    resolved = resolve_enabled_modules(chosen)
    assert resolved == REQUIRED_MODULES | frozenset(chosen)
    assert identify_preset(resolved) == PRESET_CUSTOM


def test_custom_selection_can_mix_contextual_and_weighted_keys():
    resolved = resolve_enabled_modules([ICT_FVG, VOLUME])
    assert {ICT_FVG, VOLUME} <= resolved
    assert REQUIRED_MODULES <= resolved
    assert _weight_of(resolved) == MODULE_WEIGHTS[TREND] + MODULE_WEIGHTS[STRUCTURE] + MODULE_WEIGHTS[VOLUME]


def test_a_preset_shaped_custom_selection_is_named_as_that_preset():
    # Ticking exactly the ICT+MSNR boxes by hand IS the ICT+MSNR preset.
    assert identify_preset(preset_modules(PRESET_CONSERVATIVE)) == PRESET_CONSERVATIVE
    assert identify_preset(preset_modules(PRESET_BALANCED)) == PRESET_BALANCED


def test_an_unknown_preset_id_is_rejected_not_substituted():
    """Falling back would fail OPEN: an unrecognised string is not a licence
    to run the production strategy. The literal 'custom' matters here — the API
    echoes that value in its own responses, so a client replaying a response
    would have been silently upgraded to a tradeable configuration."""
    for bad in ('not_a_preset', '', None, 'custom', 'CUSTOM'):
        with pytest.raises(UnknownPresetError):
            preset_modules(bad)


def test_known_preset_ids_still_resolve():
    for preset_id in PRESETS:
        assert preset_modules(preset_id) == frozenset(
            REQUIRED_MODULES | PRESETS[preset_id]
        )


def test_balanced_preset_is_still_every_module():
    # Backward compatibility: the historical default remains reachable.
    assert preset_modules(PRESET_BALANCED) == frozenset(ALL_MODULE_KEYS)


# ─────────────────────────────────────────────────────────
# 10–11. A preset is a real backend configuration
# ─────────────────────────────────────────────────────────

def test_presets_produce_materially_different_configurations():
    ict = preset_modules(PRESET_ICT_MSNR)
    balanced = preset_modules(PRESET_BALANCED)
    conservative = preset_modules(PRESET_CONSERVATIVE)
    assert ict != balanced != conservative
    assert _weight_of(ict) != _weight_of(balanced)
    # ICT/MSNR context is exclusive to the ICT+MSNR preset.
    assert not (conservative & frozenset(ICT_MODULES))


def test_selecting_a_preset_changes_what_the_engine_scores():
    """The proof that a preset is not cosmetic: the breakdown differs."""
    ict = {c.name for c in QualityScorer().score(
        None, _confluence(preset_modules(PRESET_ICT_MSNR))).components}
    conservative = {c.name for c in QualityScorer().score(
        None, _confluence(preset_modules(PRESET_CONSERVATIVE))).components}
    assert ict != conservative
    assert RSI in conservative and RSI not in ict


def test_resolver_accepts_every_key_a_preset_can_name():
    for preset_id, keys in PRESETS.items():
        resolved = resolve_enabled_modules(sorted(keys))
        assert resolved == preset_modules(preset_id), preset_id


# ─────────────────────────────────────────────────────────
# 12–14. The existing gates are untouched
# ─────────────────────────────────────────────────────────

def _score(value):
    return Score(value=value, grade=grade_for(value), components=[])


def test_quality_59_confidence_45_is_still_wait():
    """The audit case. A good R:R can never rescue a weak setup."""
    decision = decide(
        market_blocking=None,
        confluence=_confluence(DEFAULT_ACTIVE),
        quality=_score(59),
        confidence=_score(45),
    )
    assert decision.signal == WAIT
    assert decision.tradeable is False
    assert decision.reason_code == CODE_QUALITY_BELOW
    # The bias is still reported, so the UI can say "bullish, but no trade".
    assert decision.direction_bias == 'long'


def test_confidence_floor_still_rejects_a_high_quality_setup():
    decision = decide(
        market_blocking=None,
        confluence=_confluence(DEFAULT_ACTIVE),
        quality=_score(90),
        confidence=_score(59),
    )
    assert decision.signal == WAIT
    assert decision.reason_code == CODE_CONFIDENCE_BELOW


def test_the_floors_are_unchanged_by_the_new_default():
    assert MIN_TRADEABLE_QUALITY == 60
    assert MIN_TRADEABLE_CONFIDENCE == 60


def test_a_setup_clearing_both_floors_is_tradeable():
    decision = decide(
        market_blocking=None,
        confluence=_confluence(DEFAULT_ACTIVE),
        quality=_score(78),
        confidence=_score(72),
    )
    assert decision.tradeable is True
    assert decision.reason_code == CODE_TRADEABLE


def test_rr_gates_still_run_after_the_score_gates():
    """R:R may only be reached once quality and confidence have passed."""
    from analysis.decision import CODE_TP1_RR, reject
    rejected = reject('long', CODE_TP1_RR, 'TP1 R:R below the minimum.')
    assert rejected.tradeable is False and rejected.signal == WAIT
    # And a weak setup never even reaches the R:R gate.
    early = decide(
        market_blocking=None, confluence=_confluence(DEFAULT_ACTIVE),
        quality=_score(59), confidence=_score(99),
    )
    assert early.reason_code == CODE_QUALITY_BELOW


def test_hard_conflicts_still_veto_under_the_default_preset():
    conf = _confluence(DEFAULT_ACTIVE)
    conflicted = ConfluenceResult(
        direction=BULLISH, votes=conf.votes,
        bullish_weight=conf.bullish_weight, bearish_weight=conf.bearish_weight,
        agreement=1.0, enabled_modules=DEFAULT_ACTIVE,
        hard_conflicts=['higher timeframe (4h) is bearish, against a bullish setup'],
    )
    decision = decide(
        market_blocking=None, confluence=conflicted,
        quality=_score(95), confidence=_score(95),
    )
    assert decision.signal == WAIT


# ─────────────────────────────────────────────────────────
# 15. Probabilistic uncertainty stays neutral
# ─────────────────────────────────────────────────────────

def test_a_51_49_read_has_no_meaningful_edge():
    """51 vs 49 is a 2pp edge — far below the threshold for directional evidence."""
    assert (51.0 - 49.0) < MIN_PROBABILITY_EDGE
    assert MIN_PROBABILITY_EDGE == 10.0


def test_the_uncertainty_threshold_survives_enabling_elliott_via_custom():
    # Elliott is off by default, but Custom mode may switch it back on — the
    # uncertainty rule must still apply when it does.
    custom = resolve_enabled_modules(sorted(PRESETS[PRESET_ICT_MSNR] | {ELLIOTT}))
    assert ELLIOTT in custom
    assert MIN_PROBABILITY_EDGE == 10.0
    # A neutral Elliott vote earns partial credit, never counts as opposition.
    votes = [_vote(m) for m in MODULE_ORDER]
    votes = [_vote(ELLIOTT, 'neutral', 0.5) if v.module == ELLIOTT else v for v in votes]
    quality = QualityScorer().score(None, _confluence(custom, votes))
    elliott_component = next(c for c in quality.components if c.name == ELLIOTT)
    assert 0 < elliott_component.points < elliott_component.max_points
