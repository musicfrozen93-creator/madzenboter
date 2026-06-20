"""Tests for the controlled 2-layer recovery system with the HYBRID trigger.

Layer 2 activates on whichever occurs first: ATR(14)×2 adverse move, OR Layer 1
floating loss ≥ recovery_loss_trigger_usd ($0.50). check_recovery_trigger now
returns (next_layer, trigger_type) or None.
"""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from grid.recovery import RecoverySystem


def _basket(num_layers: int, entry: float = 100.0, side: str = 'long',
            qty: float = 1.0, atr: float = 1.0) -> Basket:
    b = Basket(symbol='TRX/USDT:USDT', side=side, atr_at_entry=atr, volatility='tier1')
    for i in range(1, num_layers + 1):
        b.add_layer(RecoveryLayer(i, entry_price=entry, margin=2.0, quantity=qty, side=side))
    return b


def test_layer2_distance_is_atr_times_two(settings: Settings):
    rec = RecoverySystem(settings)
    assert rec.layer2_distance(2.0) == 4.0


def test_atr_trigger_long(settings: Settings):
    # Isolate the ATR condition by making the loss trigger unreachable.
    settings.recovery_loss_trigger_usd = 1e9
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='long', qty=1.0)
    atr = 2.0  # distance 4.0 → trigger at 96.0
    assert rec.check_recovery_trigger(b, 96.1, atr) is None
    assert rec.check_recovery_trigger(b, 96.0, atr) == (2, 'ATR_TRIGGER')


def test_atr_trigger_short(settings: Settings):
    settings.recovery_loss_trigger_usd = 1e9   # isolate the ATR condition
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='short', qty=1.0)
    atr = 2.0  # trigger at 104.0
    assert rec.check_recovery_trigger(b, 103.9, atr) is None
    assert rec.check_recovery_trigger(b, 104.0, atr) == (2, 'ATR_TRIGGER')


def test_loss_trigger_fires_before_atr(settings: Settings):
    # No ATR distance (atr=0) → only the floating-loss condition can fire.
    settings.recovery_loss_trigger_usd = 0.50
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='long', qty=1.0)
    # L1 floating loss at 99.6 = -$0.40 (< $0.50) → no trigger.
    assert rec.check_recovery_trigger(b, 99.6, atr=0.0) is None
    # At 99.4 = -$0.60 (≥ $0.50) → LOSS_TRIGGER.
    assert rec.check_recovery_trigger(b, 99.4, atr=0.0) == (2, 'LOSS_TRIGGER')


def test_loss_trigger_example_from_spec(settings: Settings):
    # Spec example: L1 margin $2, floating loss −$0.55, ATR not reached → open L2.
    settings.recovery_loss_trigger_usd = 0.50
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='long', qty=1.0, atr=100.0)  # ATR huge → not reached
    # −$0.55 floating loss at price 99.45.
    layer, ttype = rec.check_recovery_trigger(b, 99.45, atr=100.0)
    assert layer == 2 and ttype == 'LOSS_TRIGGER'


def test_both_conditions_reported(settings: Settings):
    settings.recovery_loss_trigger_usd = 0.50
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='long', qty=1.0)
    # Deep adverse move: both ATR (distance 4 → ≤96) and loss (≥$0.50) hit.
    assert rec.check_recovery_trigger(b, 95.0, atr=2.0) == (2, 'ATR_AND_LOSS')


def test_never_a_third_layer(settings: Settings):
    settings.recovery_loss_trigger_usd = 0.50
    rec = RecoverySystem(settings)
    long_b = _basket(2, entry=100.0, side='long', qty=1.0)
    assert rec.check_recovery_trigger(long_b, 1.0, 2.0) is None
    short_b = _basket(2, entry=100.0, side='short', qty=1.0)
    assert rec.check_recovery_trigger(short_b, 10_000.0, 2.0) is None


def test_no_trigger_when_flat(settings: Settings):
    settings.recovery_loss_trigger_usd = 0.50
    rec = RecoverySystem(settings)
    b = _basket(1, entry=100.0, side='long', qty=1.0)
    # At entry, no ATR distance and no loss → None.
    assert rec.check_recovery_trigger(b, 100.0, atr=0.0) is None
