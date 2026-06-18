"""Tests for the controlled 2-layer recovery system (Layer 2 at ATR x2)."""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from grid.recovery import RecoverySystem


def _basket(num_layers: int, entry: float = 100.0, side: str = 'long', atr: float = 1.0) -> Basket:
    b = Basket(symbol='TRX/USDT:USDT', side=side, atr_at_entry=atr, volatility='normal')
    for i in range(1, num_layers + 1):
        b.add_layer(RecoveryLayer(i, entry_price=entry, margin=5.0, quantity=1.0, side=side))
    return b


def test_layer2_distance_is_atr_times_two(settings: Settings):
    rec = RecoverySystem(settings)
    # layer2_atr_multiplier default 2.0
    assert rec.layer2_distance(2.0) == 4.0


def test_layer2_triggers_long(settings: Settings):
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='long')
    atr = 2.0  # distance = 2 * 2.0 = 4.0 → trigger at 96.0
    assert rec.check_recovery_trigger(b, 96.1, atr) is None
    assert rec.check_recovery_trigger(b, 96.0, atr) == 2


def test_layer2_triggers_short(settings: Settings):
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='short')
    atr = 2.0  # trigger at 104.0
    assert rec.check_recovery_trigger(b, 103.9, atr) is None
    assert rec.check_recovery_trigger(b, 104.0, atr) == 2


def test_never_a_third_layer(settings: Settings):
    rec = RecoverySystem(settings)
    long_b = _basket(2, entry=100.0, side='long')
    assert rec.check_recovery_trigger(long_b, 1.0, 2.0) is None
    short_b = _basket(2, entry=100.0, side='short')
    assert rec.check_recovery_trigger(short_b, 10_000.0, 2.0) is None


def test_no_trigger_without_atr(settings: Settings):
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='long')
    assert rec.check_recovery_trigger(b, 50.0, 0.0) is None
