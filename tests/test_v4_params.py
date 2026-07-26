"""V4 Phase 1 — parameter auto-scaling tests (spec §C2, §C5)."""

from v4.params import V4Params, resolve_account_limits


def test_below_floor_not_tradeable():
    lim = resolve_account_limits(30.0)
    assert not lim.tradeable
    assert 'below_min_balance' in lim.reason


def test_zero_and_negative_equity_rejected():
    assert not resolve_account_limits(0.0).tradeable
    assert not resolve_account_limits(-5.0).tradeable


def test_concurrency_ladder():
    assert resolve_account_limits(50.0).max_concurrent == 1
    assert resolve_account_limits(99.99).max_concurrent == 1
    assert resolve_account_limits(100.0).max_concurrent == 2
    assert resolve_account_limits(250.0).max_concurrent == 3
    assert resolve_account_limits(500.0).max_concurrent == 3
    assert resolve_account_limits(1000.0).max_concurrent == 4
    assert resolve_account_limits(50000.0).max_concurrent == 4


def test_caps_scale_with_equity():
    p = V4Params()
    lim = resolve_account_limits(1000.0, p)
    assert lim.risk_pct == 0.01
    assert lim.max_risk_at_once_usd == 1000.0 * 0.04
    assert lim.total_notional_cap_usd == 1000.0 * 3.0
    assert lim.total_margin_cap_usd == 1000.0 * 0.40
    # margin budget per trade = total margin cap / concurrency
    assert lim.margin_budget_per_trade_usd == (1000.0 * 0.40) / 4


def test_margin_budget_is_positive_for_all_tradeable_bands():
    for eq in (50, 100, 250, 500, 1000, 10000):
        lim = resolve_account_limits(float(eq))
        assert lim.tradeable
        assert lim.margin_budget_per_trade_usd > 0
