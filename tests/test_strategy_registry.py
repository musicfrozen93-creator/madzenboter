"""The canonical strategy registry — one source of truth for every strategy.

A strategy is a NAMED SELECTION over the existing module registry. These tests
pin that it stays exactly that: no strategy may invent a module, give a
contextual toggle artificial weight, or enable evidence that nothing can score.

The coherence rules asserted here are the ones that make a strategy honest:

  * a strategy enabling ICT/MSNR toggles must enable ICT_CONFLUENCE, or those
    toggles feed nothing (measured, not assumed — see test_ict_msnr_influence);
  * a strategy enabling a contextual toggle must enable its weighted evidence
    carrier, or the evidence has no scoring path;
  * no strategy may make the same evidence score twice.
"""

import pytest

from analysis.modules import (
    ADX,
    ATR,
    CONFIRMATION_MODULES,
    ELLIOTT,
    FIBONACCI,
    FVG,
    ICT_CONFLUENCE,
    ICT_FVG,
    ICT_LIQUIDITY,
    ICT_LIQUIDITY_SWEEPS,
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
    REQUIRED_MODULES,
    RSI,
    STRUCTURE,
    SUPPORT_RESISTANCE,
    TREND,
    VOLUME,
    VWAP,
    resolve_enabled_modules,
)
from analysis.strategies import (
    CUSTOM_STRATEGY,
    CUSTOM_STRATEGY_ID,
    DEFAULT_STRATEGY_ID,
    STRATEGIES,
    STRATEGY_ORDER,
    Strategy,
    describe_strategies,
    get_strategy,
    identify_strategy,
    strategy_modules,
)


def _weight(keys):
    return sum(MODULE_WEIGHTS[m] for m in MODULE_ORDER if m in keys)


# ─────────────────────────────────────────────
# 1–5. Each strategy resolves to its expected modules
# ─────────────────────────────────────────────

def test_ict_msnr_resolves_to_the_smart_money_configuration():
    mods = strategy_modules('ICT_MSNR_V1')
    assert REQUIRED_MODULES <= mods
    assert frozenset(ICT_MODULES) <= mods
    assert frozenset(MSNR_MODULES) <= mods
    assert frozenset(CONFIRMATION_MODULES) <= mods
    assert frozenset(ICT_MSNR_CARRIERS) <= mods
    # The six modules this methodology deliberately excludes.
    assert not (mods & {ELLIOTT, FIBONACCI, RSI, MACD, ADX, PATTERN})
    assert _weight(mods) == 71


def test_trend_following_resolves_to_trend_and_momentum_modules():
    mods = strategy_modules('TREND_FOLLOWING_V1')
    assert {ADX, MACD, FIBONACCI, SUPPORT_RESISTANCE} <= mods
    assert frozenset(CONFIRMATION_MODULES) <= mods
    # No smart-money overlay.
    assert not (mods & set(ICT_MODULES))
    assert not (mods & set(MSNR_MODULES))


def test_breakout_retest_resolves_to_levels_and_displacement():
    mods = strategy_modules('BREAKOUT_RETEST_V1')
    assert {SUPPORT_RESISTANCE, PATTERN, ORDER_BLOCK, FVG, LIQUIDITY} <= mods
    assert ICT_CONFLUENCE in mods
    assert frozenset(MSNR_MODULES) <= mods
    assert frozenset(CONFIRMATION_MODULES) <= mods


def test_mean_reversion_resolves_to_range_and_overextension_modules():
    mods = strategy_modules('MEAN_REVERSION_V1')
    assert {RSI, FIBONACCI, SUPPORT_RESISTANCE, VWAP} <= mods
    assert ICT_CONFLUENCE in mods
    # Not a breakout strategy: no displacement/BOS.
    assert 'ict_displacement' not in mods
    assert 'ict_bos' not in mods


def test_momentum_resolves_to_momentum_modules():
    mods = strategy_modules('MOMENTUM_V1')
    assert {RSI, MACD, ADX} <= mods
    assert frozenset(CONFIRMATION_MODULES) <= mods
    assert not (mods & set(ICT_MODULES))
    assert not (mods & set(MSNR_MODULES))


def test_every_strategy_is_a_distinct_real_configuration():
    seen = {}
    for s in STRATEGY_ORDER:
        assert s.enabled_modules <= OPTIONAL_UI_KEYS, s.strategy_id
        assert _weight(s.modules) >= 30, f'{s.strategy_id} scores on almost nothing'
        key = frozenset(s.enabled_modules)
        assert key not in seen, f'{s.strategy_id} duplicates {seen.get(key)}'
        seen[key] = s.strategy_id


# ─────────────────────────────────────────────
# Coherence: a strategy must not under-deliver silently
# ─────────────────────────────────────────────

@pytest.mark.parametrize('strategy', STRATEGY_ORDER, ids=lambda s: s.strategy_id)
def test_contextual_toggles_always_ship_with_the_confluence(strategy):
    """ICT/MSNR toggles without ICT_CONFLUENCE feed nothing and score nothing."""
    contextual = strategy.enabled_modules & (set(ICT_MODULES) | set(MSNR_MODULES))
    if contextual - {ICT_CONFLUENCE}:
        assert ICT_CONFLUENCE in strategy.enabled_modules, strategy.strategy_id


@pytest.mark.parametrize('strategy', STRATEGY_ORDER, ids=lambda s: s.strategy_id)
def test_contextual_toggles_always_ship_with_their_carrier(strategy):
    """Evidence with no weighted carrier has no path into the score."""
    keys = strategy.enabled_modules
    if ICT_FVG in keys:
        assert FVG in keys, strategy.strategy_id
    if ICT_ORDER_BLOCKS in keys:
        assert ORDER_BLOCK in keys, strategy.strategy_id
    if keys & {ICT_LIQUIDITY, ICT_LIQUIDITY_SWEEPS}:
        assert LIQUIDITY in keys, strategy.strategy_id
    if keys & set(MSNR_MODULES):
        assert SUPPORT_RESISTANCE in keys, strategy.strategy_id


@pytest.mark.parametrize('strategy', STRATEGY_ORDER, ids=lambda s: s.strategy_id)
def test_core_modules_are_locked_never_selectable(strategy):
    assert strategy.locked_modules == frozenset(REQUIRED_MODULES)
    assert not (strategy.enabled_modules & REQUIRED_MODULES)
    assert {TREND, STRUCTURE} <= strategy.modules


# ─────────────────────────────────────────────
# 12–16. No artificial votes, no double-counting
# ─────────────────────────────────────────────

@pytest.mark.parametrize('strategy', STRATEGY_ORDER, ids=lambda s: s.strategy_id)
def test_no_strategy_gives_a_contextual_module_scoring_weight(strategy):
    for key in strategy.enabled_modules & (set(ICT_MODULES) | set(MSNR_MODULES)):
        assert MODULE_WEIGHTS.get(key, 0) == 0, f'{key} became a weighted vote'


@pytest.mark.parametrize('ict_key,carrier', [
    (ICT_FVG, FVG),
    (ICT_ORDER_BLOCKS, ORDER_BLOCK),
    (ICT_LIQUIDITY, LIQUIDITY),
])
def test_shared_evidence_keeps_exactly_one_scoring_path(ict_key, carrier):
    """Enabling the contextual view adds no weight; the carrier scores it once."""
    for strategy in STRATEGY_ORDER:
        mods = strategy.modules
        if ict_key not in mods:
            continue
        assert carrier in mods
        assert _weight(mods) == _weight(mods - {ict_key}), strategy.strategy_id


def test_support_resistance_is_the_single_path_for_msnr_zones():
    for strategy in STRATEGY_ORDER:
        if strategy.enabled_modules & set(MSNR_MODULES):
            mods = strategy.modules
            assert SUPPORT_RESISTANCE in mods
            assert _weight(mods) == _weight(mods - set(MSNR_MODULES))


def test_market_structure_is_never_duplicated_by_an_ict_key():
    """ICT's BOS/MSS reinterpret the core STRUCTURE module — they add no weight."""
    for strategy in STRATEGY_ORDER:
        mods = strategy.modules
        assert MODULE_WEIGHTS.get('ict_bos', 0) == 0
        assert MODULE_WEIGHTS.get('ict_mss', 0) == 0
        weighted = [m for m in MODULE_ORDER if m in mods]
        assert len(weighted) == len(set(weighted)), strategy.strategy_id


# ─────────────────────────────────────────────
# 6–7. Custom, and one strategy per analysis
# ─────────────────────────────────────────────

def test_custom_is_offered_but_holds_no_fixed_module_list():
    assert CUSTOM_STRATEGY.strategy_id == CUSTOM_STRATEGY_ID
    assert CUSTOM_STRATEGY.enabled_modules == frozenset()
    assert CUSTOM_STRATEGY_ID not in STRATEGIES


def test_custom_selection_is_honoured_and_named_custom():
    chosen = [RSI, MACD, VOLUME]
    resolved = resolve_enabled_modules(chosen)
    assert resolved == REQUIRED_MODULES | frozenset(chosen)
    assert identify_strategy(resolved).strategy_id == CUSTOM_STRATEGY_ID


def test_a_hand_built_selection_matching_a_strategy_is_named_as_it():
    for strategy in STRATEGY_ORDER:
        assert identify_strategy(strategy.modules).strategy_id == strategy.strategy_id


def test_resolution_yields_exactly_one_strategy_never_a_union():
    """One primary strategy per analysis — ids do not combine."""
    a = strategy_modules('ICT_MSNR_V1')
    b = strategy_modules('MOMENTUM_V1')
    # A comma-joined id is not a strategy; it falls back to the default, and in
    # particular never resolves to the union of the two.
    combined = strategy_modules('ICT_MSNR_V1,MOMENTUM_V1')
    assert combined == strategy_modules(DEFAULT_STRATEGY_ID)
    assert combined != (a | b)


def test_identify_never_reports_two_strategies():
    result = identify_strategy(strategy_modules('MOMENTUM_V1'))
    assert isinstance(result, Strategy)
    assert result.strategy_id == 'MOMENTUM_V1'


# ─────────────────────────────────────────────
# 10. The frontend cannot invent a mapping
# ─────────────────────────────────────────────

def test_an_unknown_strategy_id_falls_back_to_the_default():
    for bogus in ('NOT_A_STRATEGY', '', None, 'DROP TABLE', 'ict_msnr_v99'):
        assert strategy_modules(bogus) == strategy_modules(DEFAULT_STRATEGY_ID)


def test_get_strategy_returns_none_for_ids_it_does_not_own():
    assert get_strategy('NOPE') is None
    assert get_strategy(CUSTOM_STRATEGY_ID) is None
    assert get_strategy(None) is None


def test_strategy_ids_are_case_insensitive_but_still_registry_resolved():
    assert strategy_modules('ict_msnr_v1') == strategy_modules('ICT_MSNR_V1')


# ─────────────────────────────────────────────
# 18, 20. Default and versioning
# ─────────────────────────────────────────────

def test_the_default_strategy_is_ict_msnr_and_is_the_recommended_one():
    assert DEFAULT_STRATEGY_ID == 'ICT_MSNR_V1'
    recommended = [s for s in STRATEGY_ORDER if s.recommended]
    assert [s.strategy_id for s in recommended] == [DEFAULT_STRATEGY_ID]


def test_every_strategy_id_carries_its_version():
    for s in STRATEGY_ORDER:
        assert s.strategy_id.endswith(f'_V{s.version}'), s.strategy_id
        assert s.version >= 1


def test_the_catalogue_exposes_the_full_metadata_contract():
    for entry in describe_strategies():
        assert set(entry) == {
            'strategy_id', 'name', 'description', 'category', 'enabled_modules',
            'locked_modules', 'confirmation_modules', 'recommended', 'version',
            'status',
        }
        assert entry['name'] and entry['description'] and entry['category']


def test_descriptions_make_no_performance_claims():
    """Nothing here is backtested; the copy must not imply otherwise."""
    banned = (
        'best', 'profit', 'guarantee', 'win rate', 'highest', 'most accurate',
        'outperform', 'superior',
    )
    for entry in describe_strategies():
        text = entry['description'].lower()
        for word in banned:
            assert word not in text, f'{entry["strategy_id"]}: "{word}"'
