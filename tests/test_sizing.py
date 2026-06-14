"""Regression tests for position sizing and recovery-layer margins (CHANGE #8)."""

from config.settings import Settings, VolatilityLevel
from grid.recovery import RecoverySystem
from risk.position_sizer import PositionSizer


def _market_info(min_notional=5.0, min_qty=0.001, amount_precision=0.001):
    return {
        'limits': {'cost': {'min': min_notional}, 'amount': {'min': min_qty}},
        'precision': {'amount': amount_precision, 'price': 0.01},
    }


def test_base_margin_is_fixed_two_dollars(settings: Settings):
    sizer = PositionSizer(settings)
    for balance in (10, 50, 200, 5000):
        for vol in VolatilityLevel:
            assert sizer.calculate_base_margin(balance, vol) == 2.0


def test_recovery_layer_margins_sum_to_five(settings: Settings):
    recovery = RecoverySystem(settings)

    class _B:
        side = 'long'

    margins = []
    for layer_number in range(1, settings.recovery_max_layers + 1):
        lp = recovery.calculate_layer_params(
            _B(), layer_number, base_margin=2.0, current_price=100.0, leverage=10
        )
        margins.append(lp.margin)

    assert margins == [2.0, 1.0, 1.0, 1.0]
    assert abs(sum(margins) - 5.0) < 1e-9


def test_evaluate_entry_rejects_when_min_order_exceeds_cap(settings: Settings):
    """A high-priced coin whose smallest order needs > $5 margin is rejected."""
    sizer = PositionSizer(settings)
    # price 70000, min qty 0.001 -> notional 70, margin at 10x = 7 > $5 cap
    mi = _market_info(min_notional=100.0, min_qty=0.001, amount_precision=0.001)
    plan = sizer.evaluate_entry(
        balance=1000.0, price=70000.0, leverage=10,
        volatility=VolatilityLevel.MEDIUM, market_info=mi,
    )
    assert plan['suitable'] is False
    assert plan['margin'] > settings.max_basket_margin_usd


def test_evaluate_entry_suitable_for_cheap_liquid_coin(settings: Settings):
    sizer = PositionSizer(settings)
    mi = _market_info(min_notional=5.0, min_qty=1.0, amount_precision=1.0)
    plan = sizer.evaluate_entry(
        balance=200.0, price=0.5, leverage=10,
        volatility=VolatilityLevel.MEDIUM, market_info=mi,
    )
    assert plan['suitable'] is True
    # First-layer margin must never exceed the $5 basket cap.
    assert plan['margin'] <= settings.max_basket_margin_usd


def test_first_layer_never_exceeds_cap_across_prices(settings: Settings):
    sizer = PositionSizer(settings)
    for price in (0.01, 0.5, 5.0, 50.0, 500.0):
        mi = _market_info(min_notional=5.0, min_qty=0.001, amount_precision=0.001)
        plan = sizer.evaluate_entry(
            balance=500.0, price=price, leverage=8,
            volatility=VolatilityLevel.MEDIUM, market_info=mi,
        )
        if plan['suitable']:
            assert plan['margin'] <= settings.max_basket_margin_usd + 1e-9
