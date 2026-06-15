"""Regression tests for balance-tier position sizing and recovery-layer
margins (CHANGE #2 / #1: tiered sizing, 2-layer recovery)."""

from config.settings import Settings, VolatilityLevel
from grid.recovery import RecoverySystem
from risk.position_sizer import PositionSizer


def _market_info(min_notional=5.0, min_qty=0.001, amount_precision=0.001):
    return {
        'limits': {'cost': {'min': min_notional}, 'amount': {'min': min_qty}},
        'precision': {'amount': amount_precision, 'price': 0.01},
    }


def test_base_margin_is_tiered_by_balance(settings: Settings):
    # CHANGE #2 — Layer-1 margin is fixed per balance tier, vol-independent.
    sizer = PositionSizer(settings)
    expected = {10: 1.5, 50: 1.5, 75: 2.5, 200: 2.5, 500: 3.5, 5000: 3.5}
    for balance, l1 in expected.items():
        for vol in VolatilityLevel:
            assert sizer.calculate_base_margin(balance, vol) == l1


def test_recovery_is_two_layers_summing_to_tier_cap(settings: Settings):
    # CHANGE #1 + #2 — exactly two layers; their sum equals the tier cap.
    recovery = RecoverySystem(settings)

    class _B:
        side = 'long'

    # Maximum layers per basket is 2.
    assert settings.recovery_max_layers == 2

    cases = {
        10: ([1.5, 1.0], 2.5),    # Tier A
        100: ([2.5, 1.0], 3.5),   # Tier B
        500: ([3.5, 1.0], 4.5),   # Tier C
    }
    for balance, (expected_margins, cap) in cases.items():
        margins = []
        for layer_number in range(1, settings.recovery_max_layers + 1):
            lp = recovery.calculate_layer_params(
                _B(), layer_number, base_margin=expected_margins[0],
                current_price=100.0, leverage=10, balance=balance,
            )
            margins.append(lp.margin)
        assert margins == expected_margins
        assert abs(sum(margins) - cap) < 1e-9
        assert sum(margins) <= settings.get_margin_hard_cap(balance) + 1e-9


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
    # First-layer margin must never exceed the account tier's basket cap.
    assert plan['margin'] <= settings.get_margin_hard_cap(200.0)


def test_first_layer_never_exceeds_cap_across_prices(settings: Settings):
    sizer = PositionSizer(settings)
    for price in (0.01, 0.5, 5.0, 50.0, 500.0):
        mi = _market_info(min_notional=5.0, min_qty=0.001, amount_precision=0.001)
        plan = sizer.evaluate_entry(
            balance=500.0, price=price, leverage=8,
            volatility=VolatilityLevel.MEDIUM, market_info=mi,
        )
        if plan['suitable']:
            assert plan['margin'] <= settings.get_margin_hard_cap(500.0) + 1e-9
