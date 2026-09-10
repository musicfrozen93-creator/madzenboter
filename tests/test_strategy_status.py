"""Production-safety layer: an unvalidated strategy must never look tradeable.

ICT + MSNR is the one methodology that has actually been designed and audited
here, so it alone carries production status. The other four are compositions
over the module registry — useful for research, not derived from validated
trading rules and never backtested — so the engine analyses them in full but
refuses to make their verdicts tradeable.

The enforcement lives in ``analysis.decision``, the single BUY/SELL/WAIT
authority, and is driven by the modules the engine ACTUALLY RAN rather than by
anything the caller claimed. A client cannot bypass it by naming one strategy
and sending another's module list, or by calling the API directly.
"""

import pytest

from analysis.confluence import ConfluenceResult
from analysis.decision import (
    CODE_CUSTOM_STRATEGY,
    CODE_EXPERIMENTAL_STRATEGY,
    CODE_TRADEABLE,
    WAIT,
    decide,
)
from analysis.modules import (
    MODULE_ORDER,
    REQUIRED_MODULES,
    ModuleVote,
    resolve_enabled_modules,
)
from analysis.scoring import (
    MIN_PROBABILITY_EDGE,
    MIN_TRADEABLE_CONFIDENCE,
    MIN_TRADEABLE_QUALITY,
    Score,
    grade_for,
)
from analysis.strategies import (
    CUSTOM_STRATEGY,
    CUSTOM_STRATEGY_ID,
    DEFAULT_STRATEGY_ID,
    STRATEGIES,
    STRATEGY_ORDER,
    StrategyStatus,
    blocks_tradeable,
    describe_strategies,
    identify_strategy,
    live_signal_eligible,
    strategy_modules,
    strategy_status,
)
from analysis.structure import BULLISH

EXPERIMENTAL_IDS = [
    s.strategy_id for s in STRATEGY_ORDER if s.status == StrategyStatus.EXPERIMENTAL
]


def _score(value):
    return Score(value=value, grade=grade_for(value), components=[])


def _confluence(active, direction=BULLISH):
    votes = [
        ModuleVote(m, direction, 1.0, m, 'detail')
        for m in MODULE_ORDER if m in active
    ]
    weight = sum(v.weighted_strength for v in votes)
    return ConfluenceResult(
        direction=direction, votes=votes,
        bullish_weight=weight, bearish_weight=0.0,
        agreement=1.0, enabled_modules=frozenset(active),
    )


# ─────────────────────────────────────────────
# 1–4. The statuses themselves
# ─────────────────────────────────────────────

def test_ict_msnr_is_the_production_strategy():
    ict = STRATEGIES['ICT_MSNR_V1']
    assert ict.status == StrategyStatus.PRODUCTION
    assert ict.recommended is True
    assert ict.is_production and not ict.blocks_tradeable
    assert ict.live_signal_eligible


@pytest.mark.parametrize('strategy_id', EXPERIMENTAL_IDS)
def test_the_other_predefined_strategies_are_experimental(strategy_id):
    s = STRATEGIES[strategy_id]
    assert s.status == StrategyStatus.EXPERIMENTAL
    assert s.recommended is False
    assert s.blocks_tradeable
    assert not s.live_signal_eligible


def test_all_four_non_ict_strategies_are_experimental():
    assert sorted(EXPERIMENTAL_IDS) == sorted([
        'BREAKOUT_RETEST_V1', 'MEAN_REVERSION_V1',
        'MOMENTUM_V1', 'TREND_FOLLOWING_V1',
    ])


def test_custom_has_its_own_status_and_is_not_called_validated():
    assert CUSTOM_STRATEGY.status == StrategyStatus.CUSTOM
    assert CUSTOM_STRATEGY.recommended is False
    # Custom is a manual research configuration. Nobody validated the module
    # combination the user assembled, so it neither trades nor publishes.
    assert CUSTOM_STRATEGY.blocks_tradeable
    assert not CUSTOM_STRATEGY.live_signal_eligible


def test_only_the_three_defined_statuses_exist():
    assert StrategyStatus.ALL == {'production', 'experimental', 'custom'}
    for s in list(STRATEGY_ORDER) + [CUSTOM_STRATEGY]:
        assert s.status in StrategyStatus.ALL


def test_no_strategy_is_deleted_definitions_remain_available():
    """Experimental strategies stay in the registry for development and testing."""
    assert len(STRATEGY_ORDER) == 5
    for strategy_id in EXPERIMENTAL_IDS:
        assert strategy_modules(strategy_id), strategy_id


def test_only_a_production_strategy_may_be_recommended():
    for s in STRATEGY_ORDER:
        if s.recommended:
            assert s.is_production, s.strategy_id
    assert STRATEGIES[DEFAULT_STRATEGY_ID].is_production


# ─────────────────────────────────────────────
# 5–9. Experimental strategies analyse, but never trade
# ─────────────────────────────────────────────

@pytest.mark.parametrize('strategy_id', EXPERIMENTAL_IDS)
def test_an_experimental_strategy_is_still_analysed(strategy_id):
    """Research needs the full diagnostic — bias and scores are still produced."""
    active = strategy_modules(strategy_id)
    decision = decide(
        market_blocking=None, confluence=_confluence(active),
        quality=_score(95), confidence=_score(95),
    )
    # The bias is recorded, so a researcher can see which way it leaned.
    assert decision.direction_bias == 'long'
    assert decision.reason_message


@pytest.mark.parametrize('strategy_id', EXPERIMENTAL_IDS)
def test_an_experimental_strategy_can_never_be_tradeable(strategy_id):
    active = strategy_modules(strategy_id)
    for quality, confidence in [(95, 95), (100, 100), (78, 72), (61, 61)]:
        decision = decide(
            market_blocking=None, confluence=_confluence(active),
            quality=_score(quality), confidence=_score(confidence),
        )
        assert decision.tradeable is False, (strategy_id, quality, confidence)
        assert decision.signal == WAIT
        assert decision.reason_code == CODE_EXPERIMENTAL_STRATEGY


@pytest.mark.parametrize('strategy_id', EXPERIMENTAL_IDS)
@pytest.mark.parametrize('direction', [BULLISH, 'bearish'])
def test_an_experimental_strategy_produces_neither_buy_nor_sell(strategy_id, direction):
    decision = decide(
        market_blocking=None,
        confluence=_confluence(strategy_modules(strategy_id), direction),
        quality=_score(99), confidence=_score(99),
    )
    assert decision.signal not in ('BUY', 'SELL')
    assert decision.signal == WAIT


@pytest.mark.parametrize('strategy_id', EXPERIMENTAL_IDS)
def test_the_wait_reason_names_the_experimental_status(strategy_id):
    decision = decide(
        market_blocking=None, confluence=_confluence(strategy_modules(strategy_id)),
        quality=_score(90), confidence=_score(90),
    )
    assert decision.reason_code == CODE_EXPERIMENTAL_STRATEGY
    assert 'experimental' in decision.reason_message.lower()
    assert STRATEGIES[strategy_id].name in decision.reason_message


def test_the_status_gate_runs_before_every_other_gate():
    """No later gate can overturn it, and it needs no other gate to fire."""
    active = strategy_modules('MOMENTUM_V1')
    # Perfect scores, clean market, no conflicts — still WAIT.
    decision = decide(
        market_blocking=None, confluence=_confluence(active),
        quality=_score(100), confidence=_score(100),
    )
    assert decision.reason_code == CODE_EXPERIMENTAL_STRATEGY


def test_the_gate_cannot_be_bypassed_by_a_hand_built_module_list():
    """Status is derived from what RAN, not from what the caller named."""
    # A client sends Momentum's exact modules without naming the strategy.
    smuggled = resolve_enabled_modules(sorted(strategy_modules('MOMENTUM_V1')))
    assert identify_strategy(smuggled).strategy_id == 'MOMENTUM_V1'
    assert blocks_tradeable(smuggled)
    decision = decide(
        market_blocking=None, confluence=_confluence(smuggled),
        quality=_score(99), confidence=_score(99),
    )
    assert decision.tradeable is False
    assert decision.reason_code == CODE_EXPERIMENTAL_STRATEGY


# ─────────────────────────────────────────────
# 11–12. Production and Custom are unaffected
# ─────────────────────────────────────────────

def test_ict_msnr_still_produces_normal_verdicts():
    active = strategy_modules('ICT_MSNR_V1')
    good = decide(
        market_blocking=None, confluence=_confluence(active),
        quality=_score(78), confidence=_score(72),
    )
    assert good.tradeable is True
    assert good.signal == 'BUY'
    assert good.reason_code == CODE_TRADEABLE


def test_ict_msnr_still_respects_the_existing_gates():
    """The audit case: 59/45 remains WAIT for the production strategy too."""
    active = strategy_modules('ICT_MSNR_V1')
    weak = decide(
        market_blocking=None, confluence=_confluence(active),
        quality=_score(59), confidence=_score(45),
    )
    assert weak.signal == WAIT
    assert weak.reason_code == 'quality_below_threshold'


def test_custom_runs_a_full_analysis_but_never_trades():
    """Custom analyses in full — bias and scores included — and returns WAIT."""
    custom = resolve_enabled_modules(['rsi', 'macd', 'volume'])
    assert strategy_status(custom) == StrategyStatus.CUSTOM
    assert blocks_tradeable(custom)
    decision = decide(
        market_blocking=None, confluence=_confluence(custom),
        quality=_score(78), confidence=_score(72),
    )
    assert decision.tradeable is False
    assert decision.signal == WAIT
    assert decision.reason_code == CODE_CUSTOM_STRATEGY
    # The directional read is still reported, which is what makes it useful
    # for research and configuration.
    assert decision.direction_bias == 'long'


def test_custom_reports_a_distinct_reason_from_experimental():
    custom = resolve_enabled_modules(['rsi', 'macd', 'volume'])
    custom_decision = decide(
        market_blocking=None, confluence=_confluence(custom),
        quality=_score(90), confidence=_score(90),
    )
    experimental = decide(
        market_blocking=None, confluence=_confluence(strategy_modules('MOMENTUM_V1')),
        quality=_score(90), confidence=_score(90),
    )
    assert custom_decision.reason_code == CODE_CUSTOM_STRATEGY
    assert experimental.reason_code == CODE_EXPERIMENTAL_STRATEGY
    assert custom_decision.reason_code != experimental.reason_code
    assert 'custom' in custom_decision.reason_message.lower()


def test_no_declared_custom_configuration_can_trade_at_any_score():
    """Every DECLARED custom selection is WAIT-only, whatever its modules.

    The dangerous input is the last one: a hand-built selection that happens to
    equal the production strategy's module set. An earlier version of this test
    skipped exactly that case with a `continue`, so it certified the claim only
    over inputs already known to pass. Declaring CUSTOM is now honoured, so the
    case is asserted rather than skipped.
    """
    for modules in (
        ['rsi'], ['macd', 'volume'], ['elliott', 'fibonacci', 'pattern'],
        sorted(resolve_enabled_modules(None)),                    # every module
        sorted(STRATEGIES['ICT_MSNR_V1'].enabled_modules),        # == production
    ):
        active = resolve_enabled_modules(modules)
        conf = _confluence(active)
        conf.declared_strategy_id = CUSTOM_STRATEGY_ID
        assert strategy_status(active, CUSTOM_STRATEGY_ID) == StrategyStatus.CUSTOM, modules
        decision = decide(
            market_blocking=None, confluence=conf,
            quality=_score(100), confidence=_score(100),
        )
        assert decision.tradeable is False, modules
        assert decision.signal == WAIT, modules
        assert decision.reason_code == CODE_CUSTOM_STRATEGY, modules


def test_a_hand_built_set_matching_production_is_custom_when_declared_custom():
    """The exact hole the audit found: one UI click, no checkbox touched."""
    production_modules = resolve_enabled_modules(
        sorted(STRATEGIES['ICT_MSNR_V1'].enabled_modules)
    )
    # Undeclared, it IS the production methodology — same modules, same analysis.
    assert identify_strategy(production_modules).strategy_id == 'ICT_MSNR_V1'
    # Declared CUSTOM, it stays a research configuration.
    assert identify_strategy(production_modules, CUSTOM_STRATEGY_ID).strategy_id == (
        CUSTOM_STRATEGY_ID
    )
    assert blocks_tradeable(production_modules, CUSTOM_STRATEGY_ID)


def test_declaring_a_strategy_can_only_ever_restrict():
    """A declaration must never make a result MORE permissive."""
    custom_modules = resolve_enabled_modules(['rsi', 'macd'])
    # Declaring the production strategy over a custom module set does not
    # launder it into production.
    assert identify_strategy(custom_modules, 'ICT_MSNR_V1').strategy_id == CUSTOM_STRATEGY_ID
    assert blocks_tradeable(custom_modules, 'ICT_MSNR_V1')
    # An unrecognised declaration is ignored, falling back to the modules.
    assert identify_strategy(custom_modules, 'NONSENSE').strategy_id == CUSTOM_STRATEGY_ID


def test_identify_strategy_requires_the_locked_core():
    """An optional-only set is not the production strategy.

    Reachable by any caller that reaches the engine without going through the
    HTTP layer — a scanner, a batch job, a backtest harness. Missing trend and
    structure is 30 of the 100 quality points.
    """
    optional_only = frozenset(STRATEGIES['ICT_MSNR_V1'].enabled_modules)
    assert not (optional_only & REQUIRED_MODULES)
    assert identify_strategy(optional_only).strategy_id == CUSTOM_STRATEGY_ID
    assert blocks_tradeable(optional_only)


def test_production_is_the_only_tradeable_status():
    assert StrategyStatus.ALL - StrategyStatus.NOT_TRADEABLE == {
        StrategyStatus.PRODUCTION
    }


def test_ict_msnr_is_the_only_strategy_that_can_trade():
    tradeable = [
        s.strategy_id for s in STRATEGY_ORDER if not s.blocks_tradeable
    ]
    assert tradeable == ['ICT_MSNR_V1']
    assert CUSTOM_STRATEGY.blocks_tradeable


def test_custom_is_still_kept_out_of_the_live_feed():
    custom = resolve_enabled_modules(['rsi', 'macd', 'volume'])
    assert not live_signal_eligible(custom)


# ─────────────────────────────────────────────
# 10. Live-signal eligibility
# ─────────────────────────────────────────────

def test_only_production_is_live_signal_eligible():
    assert live_signal_eligible(strategy_modules('ICT_MSNR_V1'))
    for strategy_id in EXPERIMENTAL_IDS:
        assert not live_signal_eligible(strategy_modules(strategy_id)), strategy_id
    assert StrategyStatus.LIVE_SIGNAL_ELIGIBLE == {StrategyStatus.PRODUCTION}


# ─────────────────────────────────────────────
# 15–16. Versioning and catalogue exposure
# ─────────────────────────────────────────────

def test_the_catalogue_publishes_the_status():
    entries = {e['strategy_id']: e for e in describe_strategies()}
    assert entries['ICT_MSNR_V1']['status'] == 'production'
    assert entries['ICT_MSNR_V1']['recommended'] is True
    for strategy_id in EXPERIMENTAL_IDS:
        assert entries[strategy_id]['status'] == 'experimental'
        assert entries[strategy_id]['recommended'] is False
    assert entries['CUSTOM']['status'] == 'custom'


def test_versions_are_preserved_alongside_status():
    for entry in describe_strategies():
        assert entry['version'] >= 1
        if entry['strategy_id'] != 'CUSTOM':
            assert entry['strategy_id'].endswith(f'_V{entry["version"]}')


def test_no_description_claims_validation_or_profitability():
    banned = (
        'best', 'profit', 'guarantee', 'proven', 'validated', 'accurate',
        'high accuracy', 'win rate', 'outperform', 'reliable',
    )
    for entry in describe_strategies():
        text = entry['description'].lower()
        for word in banned:
            assert word not in text, f'{entry["strategy_id"]}: "{word}"'


# ─────────────────────────────────────────────
# 18. Scoring is untouched by this task
# ─────────────────────────────────────────────

def test_the_thresholds_are_unchanged():
    assert MIN_TRADEABLE_QUALITY == 60
    assert MIN_TRADEABLE_CONFIDENCE == 60
    assert MIN_PROBABILITY_EDGE == 10.0


def test_ict_msnr_module_configuration_is_unchanged():
    """The audited configuration must survive this task byte for byte."""
    from analysis.modules import MODULE_WEIGHTS
    mods = strategy_modules('ICT_MSNR_V1')
    assert len(mods) == 25
    assert sum(MODULE_WEIGHTS[m] for m in MODULE_ORDER if m in mods) == 71
