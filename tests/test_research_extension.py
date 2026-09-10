"""The research extension point records signals; it never invents results.

Section 17: prepare, don't fake. These tests pin that the module carries the
metadata a future validation needs, insists on a complete study before a
strategy could be promoted, and reports "not measured" honestly.
"""

from analysis.research import (
    REQUIRED_BREAKDOWNS,
    REQUIRED_CONDITIONS,
    REQUIRED_METRICS,
    SignalRecord,
    ValidationReport,
    unvalidated,
)
from analysis.strategies import STRATEGY_ORDER, StrategyStatus


def test_the_required_metrics_cover_the_whole_picture():
    for metric in (
        'sample_size', 'win_rate', 'average_r', 'expectancy', 'profit_factor',
        'max_drawdown', 'tp1_hit_rate', 'tp2_hit_rate', 'tp3_hit_rate',
        'sl_rate', 'average_holding_time', 'signal_frequency',
    ):
        assert metric in REQUIRED_METRICS


def test_a_validation_must_break_results_down_by_market_timeframe_and_regime():
    assert set(REQUIRED_BREAKDOWNS) == {'market', 'timeframe', 'regime'}


def test_the_methodological_conditions_are_named():
    for condition in (
        'out_of_sample', 'walk_forward', 'realistic_fees', 'realistic_spread',
        'realistic_slippage', 'no_look_ahead',
    ):
        assert condition in REQUIRED_CONDITIONS


def test_a_signal_record_carries_everything_a_backtest_needs():
    record = SignalRecord(
        strategy_id='MOMENTUM_V1', strategy_version=1,
        strategy_status=StrategyStatus.EXPERIMENTAL,
        market='crypto', symbol='BTCUSDT', timeframe='15m',
        direction='WAIT', tradeable=False,
    )
    data = record.as_dict()
    for field in (
        'strategy_id', 'strategy_version', 'strategy_status', 'market', 'symbol',
        'timeframe', 'direction', 'entry', 'stop_loss', 'take_profits',
        'quality', 'confidence', 'regime', 'generated_at', 'outcome',
    ):
        assert field in data, field


def test_a_new_record_has_no_outcome():
    """The result of a trade is unknown at generation time — never invented."""
    record = SignalRecord(
        strategy_id='MOMENTUM_V1', strategy_version=1,
        strategy_status=StrategyStatus.EXPERIMENTAL,
        market='crypto', symbol='BTCUSDT', timeframe='15m',
        direction='WAIT', tradeable=False,
    )
    assert record.outcome is None


def test_every_strategy_is_currently_unvalidated():
    """No performance has been measured for anything in this codebase."""
    for strategy in STRATEGY_ORDER:
        report = unvalidated(strategy.strategy_id, strategy.version)
        assert report.metrics == {}
        assert not report.is_sufficient
        assert report.missing_metrics == list(REQUIRED_METRICS)


def test_an_incomplete_study_can_never_justify_promotion():
    partial = ValidationReport(
        strategy_id='MOMENTUM_V1', strategy_version=1,
        metrics={'win_rate': 0.61},          # one flattering number
        breakdowns={'market': {}},
        conditions={'out_of_sample': True},
    )
    assert not partial.is_sufficient
    assert 'expectancy' in partial.missing_metrics
    assert 'regime' in partial.missing_breakdowns
    assert 'walk_forward' in partial.unmet_conditions


def test_a_complete_study_is_recognised_as_sufficient():
    complete = ValidationReport(
        strategy_id='MOMENTUM_V1', strategy_version=1,
        metrics={m: 0 for m in REQUIRED_METRICS},
        breakdowns={b: {} for b in REQUIRED_BREAKDOWNS},
        conditions={c: True for c in REQUIRED_CONDITIONS},
    )
    assert complete.is_sufficient


def test_a_failed_condition_blocks_sufficiency():
    study = ValidationReport(
        strategy_id='MOMENTUM_V1', strategy_version=1,
        metrics={m: 0 for m in REQUIRED_METRICS},
        breakdowns={b: {} for b in REQUIRED_BREAKDOWNS},
        conditions={**{c: True for c in REQUIRED_CONDITIONS}, 'no_look_ahead': False},
    )
    assert not study.is_sufficient
    assert study.unmet_conditions == ['no_look_ahead']


def test_the_module_stores_no_performance_figures():
    """Nothing here may ship a number that looks like measured performance."""
    import analysis.research as research
    for name in dir(research):
        if name.startswith('_'):
            continue
        value = getattr(research, name)
        assert not isinstance(value, float), f'{name} looks like a hardcoded metric'
