"""Regression tests for TP target and trailing profit protection (CHANGE #6)."""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from grid.take_profit import TakeProfitManager


def _long_basket() -> Basket:
    """Long basket where ROI == (price - 100): margin=1, qty=1, entry=100."""
    b = Basket(symbol='X/USDT:USDT', side='long', atr_at_entry=1.0, volatility='medium')
    b.add_layer(RecoveryLayer(1, entry_price=100.0, margin=1.0, quantity=1.0, side='long'))
    return b


def _price_for_roi(roi: float) -> float:
    # With margin=1, qty=1, entry=100: ROI fraction == price - 100
    return 100.0 + roi


def test_basket_tp_fires_at_15pct(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _long_basket()
    assert tp.check_basket_tp(b, _price_for_roi(0.15)) is True
    assert tp.check_basket_tp(b, _price_for_roi(0.149)) is False


def test_profit_protection_example_locks_at_8(settings: Settings):
    """Sequence 0→5→10→13→12→11→9→8 must close at 8 (the floor)."""
    tp = TakeProfitManager(settings)
    b = _long_basket()
    sequence = [0.0, 0.05, 0.10, 0.13, 0.12, 0.11, 0.09, 0.08]
    armed = False
    closed_at = None
    for roi in sequence:
        should_close, armed = tp.evaluate_profit_protection(b, _price_for_roi(roi), armed)
        if should_close:
            closed_at = roi
            break
    assert armed is True
    assert closed_at == 0.08


def test_profit_protection_not_armed_below_10(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _long_basket()
    # Rises to 9% then falls to 8% — never armed, so never closes on protection.
    armed = False
    for roi in (0.0, 0.05, 0.09, 0.08, 0.02):
        should_close, armed = tp.evaluate_profit_protection(b, _price_for_roi(roi), armed)
        assert should_close is False
    assert armed is False


def test_profit_protection_runs_to_tp(settings: Settings):
    """Sequence 0→5→10→12→15: protection arms but TP target carries it to 15."""
    tp = TakeProfitManager(settings)
    b = _long_basket()
    armed = False
    for roi in (0.0, 0.05, 0.10, 0.12):
        should_close, armed = tp.evaluate_profit_protection(b, _price_for_roi(roi), armed)
        assert should_close is False  # never fell back to floor
    assert armed is True
    # At 15% the basket TP closes it outright.
    assert tp.check_basket_tp(b, _price_for_roi(0.15)) is True


def test_armed_state_is_sticky(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _long_basket()
    # Already armed; a small dip above floor stays open but armed.
    should_close, armed = tp.evaluate_profit_protection(b, _price_for_roi(0.095), True)
    assert armed is True
    assert should_close is False
    # Falling to the floor while armed → close.
    should_close, armed = tp.evaluate_profit_protection(b, _price_for_roi(0.08), True)
    assert should_close is True


def test_short_basket_profit_protection(settings: Settings):
    """Profit protection works symmetrically for shorts."""
    tp = TakeProfitManager(settings)
    b = Basket(symbol='X/USDT:USDT', side='short', atr_at_entry=1.0, volatility='medium')
    b.add_layer(RecoveryLayer(1, entry_price=100.0, margin=1.0, quantity=1.0, side='short'))
    # Short ROI == (100 - price). roi 0.11 -> price 99.89 (arms); floor 0.08 -> 99.92
    _, armed = tp.evaluate_profit_protection(b, 99.89, False)
    assert armed is True
    should_close, armed = tp.evaluate_profit_protection(b, 99.92, armed)
    assert should_close is True
