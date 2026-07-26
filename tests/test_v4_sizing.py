"""V4 Phase 1 — Position Sizing + Dynamic Leverage tests (spec §C1, §A1).

The key property under test is that the risk stack is MATHEMATICALLY CLOSED:
across balances and stop distances, a suitable plan never breaches the leverage
ceiling, the per-trade margin budget, or the portfolio caps — and rejections fire
with exact reasons when it cannot.
"""

import math

from v4.params import V4Params, resolve_account_limits
from v4.sizing import OpenExposure, PositionSizerV4, derive_leverage


def _market(min_qty=0.0, min_notional=5.0, amount_precision=3, price_precision=4):
    return {
        'precision': {'amount': amount_precision, 'price': price_precision},
        'limits': {'amount': {'min': min_qty}, 'cost': {'min': min_notional}},
    }


def test_derive_leverage_basic():
    # $1000 tightest (1%) stop: notional = 1x equity = 1000, budget = 100 → 10x.
    assert derive_leverage(1000.0, 100.0, 10) == 10
    # 2% stop: notional 500, budget 100 → 5x.
    assert derive_leverage(500.0, 100.0, 10) == 5
    # Cannot fit even at max leverage → 0 (reject).
    assert derive_leverage(1500.0, 100.0, 10) == 0


def test_math_closes_worst_case_tight_stop_1000():
    """1% (floored) stop on a $1000 account must fit within 10x and the margin cap."""
    p = V4Params()
    sizer = PositionSizerV4(p)
    lim = resolve_account_limits(1000.0, p)
    entry = 100.0
    stop = 99.0  # exactly 1% (the floor)
    plan = sizer.build_plan(1000.0, 'long', entry, stop, _market(), limits=lim)
    assert plan.suitable, plan.reason
    assert plan.leverage <= p.max_leverage
    assert plan.margin <= lim.margin_budget_per_trade_usd + 1e-6
    # Risk is ~1% of equity.
    assert abs(plan.risk_usd - 10.0) < 0.5
    # Notional ≈ risk / stop% = 10 / 0.01 = 1000 = 1x equity.
    assert abs(plan.notional - 1000.0) < 20.0


def test_full_book_respects_portfolio_caps():
    """Four concurrent trades on $1000 must stay within total risk/notional/margin."""
    p = V4Params()
    sizer = PositionSizerV4(p)
    lim = resolve_account_limits(1000.0, p)
    entry, stop = 100.0, 98.0  # 2% stop
    open_exp = OpenExposure()
    plans = []
    for _ in range(lim.max_concurrent):
        plan = sizer.build_plan(
            1000.0, 'long', entry, stop, _market(), open_exposure=open_exp, limits=lim
        )
        assert plan.suitable, plan.reason
        plans.append(plan)
        open_exp = OpenExposure(
            risk_usd=open_exp.risk_usd + plan.risk_usd,
            notional_usd=open_exp.notional_usd + plan.notional,
            margin_usd=open_exp.margin_usd + plan.margin,
            open_count=open_exp.open_count + 1,
        )
    assert open_exp.risk_usd <= lim.max_risk_at_once_usd + 1e-6
    assert open_exp.notional_usd <= lim.total_notional_cap_usd + 1e-6
    assert open_exp.margin_usd <= lim.total_margin_cap_usd + 1e-6
    # The (max_concurrent+1)th trade is refused on the concurrency cap.
    extra = sizer.build_plan(
        1000.0, 'long', entry, stop, _market(), open_exposure=open_exp, limits=lim
    )
    assert not extra.suitable
    assert 'max_concurrent_reached' in extra.reason


def test_stop_too_wide_is_skipped():
    sizer = PositionSizerV4()
    # 8% stop exceeds the 6% ceiling → skip.
    plan = sizer.build_plan(500.0, 'long', 100.0, 92.0, _market())
    assert not plan.suitable
    assert 'stop_too_wide' in plan.reason


def test_stop_wrong_side_rejected():
    sizer = PositionSizerV4()
    assert not sizer.build_plan(500.0, 'long', 100.0, 101.0, _market()).suitable
    assert not sizer.build_plan(500.0, 'short', 100.0, 99.0, _market()).suitable


def test_small_account_min_notional_bump():
    """A small account below min-notional at 1% risk gets bumped to ≤1.5% to clear it."""
    p = V4Params()
    sizer = PositionSizerV4(p)
    # $50 account, 3% stop, and a $20 exchange min-notional.
    # Base 1%: risk $0.50, notional = 0.50/0.03 ≈ $16.7 < $20 → bump to 1.5%:
    # risk $0.75, notional = 0.75/0.03 = $25 ≥ $20 → suitable.
    mkt = _market(min_notional=20.0)
    plan = sizer.build_plan(50.0, 'long', 100.0, 97.0, mkt)
    assert plan.suitable, plan.reason
    assert plan.risk_pct_used == p.small_account_max_risk_pct
    assert plan.notional >= 20.0


def test_below_min_notional_even_after_bump_rejected():
    p = V4Params()
    sizer = PositionSizerV4(p)
    # $50, 5% stop, $20 min-notional: even at 1.5% risk notional = 0.75/0.05 = $15 < $20.
    mkt = _market(min_notional=20.0)
    plan = sizer.build_plan(50.0, 'long', 100.0, 95.0, mkt)
    assert not plan.suitable
    assert 'below_min_notional' in plan.reason


def test_leverage_never_exceeds_ceiling_across_grid():
    """Sweep balances × stop distances; any suitable plan obeys the ceiling + caps."""
    p = V4Params()
    sizer = PositionSizerV4(p)
    for equity in (50, 100, 250, 500, 1000, 5000):
        lim = resolve_account_limits(float(equity), p)
        for stop_pct in (0.01, 0.015, 0.02, 0.03, 0.05, 0.06):
            entry = 100.0
            stop = entry * (1 - stop_pct)
            plan = sizer.build_plan(
                float(equity), 'long', entry, stop, _market(min_notional=5.0), limits=lim
            )
            if plan.suitable:
                assert 1 <= plan.leverage <= p.max_leverage
                assert plan.margin <= lim.margin_budget_per_trade_usd + 1e-6
                assert plan.risk_usd <= lim.max_risk_at_once_usd + 1e-6
