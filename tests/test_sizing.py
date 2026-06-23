"""Exchange-safety tests for PositionSizer.build_order.

Validates minimum notional, minimum quantity, step size, and precision before an
order would ever be sent — rejecting with an exact reason when invalid.
"""

from config.settings import Settings
from risk.position_sizer import PositionSizer


def _market(min_qty=0.0, min_notional=5.0, amount_precision=0):
    return {
        'precision': {'amount': amount_precision, 'price': 4},
        'limits': {'amount': {'min': min_qty}, 'cost': {'min': min_notional}},
    }


def test_valid_order_is_suitable(settings: Settings):
    sizer = PositionSizer(settings)
    plan = sizer.build_order(margin=2.0, price=0.10, leverage=5, market_info=_market())
    assert plan['suitable']
    assert plan['quantity'] == 100.0          # 2 × 5 / 0.10, floored to step
    assert plan['reason'] == 'OK'


def test_step_size_floors_quantity(settings: Settings):
    sizer = PositionSizer(settings)
    # amount precision 0 → integer lot step; 105.x floors to 105.
    plan = sizer.build_order(margin=2.1, price=0.10, leverage=5, market_info=_market())
    assert plan['quantity'] == 105.0


def test_below_min_quantity_rejected(settings: Settings):
    sizer = PositionSizer(settings)
    plan = sizer.build_order(
        margin=2.0, price=0.10, leverage=5, market_info=_market(min_qty=1000.0)
    )
    assert not plan['suitable']
    assert 'min quantity' in plan['reason']


def test_below_min_notional_rejected(settings: Settings):
    sizer = PositionSizer(settings)
    # Tiny margin → notional below the $5 exchange minimum.
    plan = sizer.build_order(
        margin=0.5, price=0.10, leverage=5, market_info=_market(min_notional=50.0)
    )
    assert not plan['suitable']
    assert 'min notional' in plan['reason']


def test_quantity_rounds_to_zero_rejected(settings: Settings):
    sizer = PositionSizer(settings)
    # High-priced asset with an integer lot step: 2×5/100000 < 1 → floors to 0.
    plan = sizer.build_order(margin=2.0, price=100000.0, leverage=5, market_info=_market())
    assert not plan['suitable']
    assert 'rounds to zero' in plan['reason']


def test_invalid_price_or_leverage_rejected(settings: Settings):
    sizer = PositionSizer(settings)
    assert not sizer.build_order(2.0, 0.0, 5, _market())['suitable']
    assert not sizer.build_order(2.0, 0.10, 0, _market())['suitable']
