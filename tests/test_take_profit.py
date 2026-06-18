"""Tests for the fixed-dollar basket take-profit.

Uses realistic low-priced-coin sizing (entry ≈ $0.10, fixed margin × leverage /
price) so the estimated round-trip fee is tiny relative to the dollar target —
exactly as it is for TRX/XRP/XLM at 5×.
"""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from grid.take_profit import TakeProfitManager

ENTRY = 0.10
LEVERAGE = 5


def _qty(margin: float) -> float:
    return (margin * LEVERAGE) / ENTRY


def _basket(margins, side='long') -> Basket:
    b = Basket(symbol='XRP/USDT:USDT', side=side, atr_at_entry=0.001, volatility='normal')
    for i, margin in enumerate(margins, start=1):
        b.add_layer(RecoveryLayer(i, entry_price=ENTRY, margin=margin, quantity=_qty(margin), side=side))
    return b


def test_target_depends_on_layer_count(settings: Settings):
    tp = TakeProfitManager(settings)
    assert tp.target_usd(_basket([5.0])) == settings.basket_tp_layer1_usd
    assert tp.target_usd(_basket([5.0, 10.0])) == settings.basket_tp_recovery_usd


def test_layer1_closes_at_half_dollar(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _basket([5.0], side='long')  # qty = 250 @ 0.10
    assert not tp.check_basket_tp(b, ENTRY)            # 0 profit
    # +0.0025 move on 250 qty = +$0.625 gross, fees ≈ $0.05 → net ≈ $0.57 ≥ $0.50.
    assert tp.check_basket_tp(b, ENTRY + 0.0025)


def test_recovery_requires_bigger_profit(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _basket([5.0, 10.0], side='long')  # qty = 250 + 500 = 750 @ 0.10
    # +0.001 move = +$0.75 gross (< $1.75 target) → no close.
    assert not tp.check_basket_tp(b, ENTRY + 0.001)
    # +0.004 move = +$3.00 gross → net well above $1.75 → close.
    assert tp.check_basket_tp(b, ENTRY + 0.004)


def test_short_side_profit(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _basket([5.0], side='short')
    assert not tp.check_basket_tp(b, ENTRY)
    assert tp.check_basket_tp(b, ENTRY - 0.0025)       # price down → short profit
