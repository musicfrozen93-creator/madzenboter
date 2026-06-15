"""Regression tests for the 2-layer recovery system (CHANGE #1).

Maximum layers per basket = 2 (Layer 1 initial entry + Layer 2 single recovery
layer). The Layer-2 trigger spacing (0.75 × ATR from the Layer-1 entry) is
preserved unchanged.
"""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from grid.recovery import RecoverySystem


def _basket(num_layers: int, entry: float = 100.0, side: str = 'long') -> Basket:
    b = Basket(symbol='X/USDT:USDT', side=side, atr_at_entry=1.0, volatility='medium')
    for i in range(1, num_layers + 1):
        b.add_layer(RecoveryLayer(i, entry_price=entry, margin=1.0, quantity=1.0, side=side))
    return b


def test_max_layers_is_two(settings: Settings):
    assert settings.recovery_max_layers == 2


def test_layer2_triggers_at_075_atr_long(settings: Settings):
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='long')
    atr = 2.0  # 0.75 * 2.0 = 1.5 below entry → trigger at 98.5
    assert rec.check_recovery_trigger(b, 98.6, atr) is None
    assert rec.check_recovery_trigger(b, 98.5, atr) == 2


def test_layer2_triggers_at_075_atr_short(settings: Settings):
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='short')
    atr = 2.0  # 0.75 * 2.0 = 1.5 above entry → trigger at 101.5
    assert rec.check_recovery_trigger(b, 101.4, atr) is None
    assert rec.check_recovery_trigger(b, 101.5, atr) == 2


def test_no_third_layer_ever(settings: Settings):
    rec = RecoverySystem(settings)
    # Basket already has 2 layers → next would be Layer 3 → never triggers,
    # even at an extreme adverse price.
    b_long = _basket(2, entry=100.0, side='long')
    assert rec.check_recovery_trigger(b_long, 1.0, 2.0) is None
    b_short = _basket(2, entry=100.0, side='short')
    assert rec.check_recovery_trigger(b_short, 10_000.0, 2.0) is None


def test_layer2_margin_is_tier_recovery_layer(settings: Settings):
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='long')
    # Tier A ($25) → L2 $1.00; Tier C ($1000) → L2 $1.00 (recovery layer is $1).
    for balance in (25.0, 150.0, 1000.0):
        lp = rec.calculate_layer_params(
            b, 2, base_margin=1.5, current_price=98.5, leverage=10, balance=balance,
        )
        assert lp.margin == 1.0
        assert lp.layer_number == 2
