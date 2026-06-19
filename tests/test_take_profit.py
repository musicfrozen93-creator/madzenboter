"""Tests for the fixed-dollar, tier-specific basket take-profit.

Uses realistic low-priced-coin sizing (entry ≈ $0.10, fixed margin × leverage /
price) so the estimated round-trip fee is tiny relative to the dollar target —
exactly as it is for TRX/XRP/XLM at 5×. The basket's tier is stored in the
``volatility`` field (where the position manager locks it at open).
"""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from grid.take_profit import TakeProfitManager

ENTRY = 0.10
LEVERAGE = 5


def _qty(margin: float) -> float:
    return (margin * LEVERAGE) / ENTRY


def _basket(margins, side='long', tier='tier1') -> Basket:
    b = Basket(symbol='XRP/USDT:USDT', side=side, atr_at_entry=0.001, volatility=tier)
    for i, margin in enumerate(margins, start=1):
        b.add_layer(RecoveryLayer(i, entry_price=ENTRY, margin=margin, quantity=_qty(margin), side=side))
    return b


def test_target_uses_basket_tier_and_layer_count(settings: Settings):
    tp = TakeProfitManager(settings)
    # Tier 1: $0.50 (L1) / $1.50 (L1+L2)
    assert tp.target_usd(_basket([2.0], tier='tier1')) == 0.50
    assert tp.target_usd(_basket([2.0, 4.0], tier='tier1')) == 1.50
    # Tier 2: $0.80 (L1) / $2.00 (L1+L2)
    assert tp.target_usd(_basket([4.0], tier='tier2')) == 0.80
    assert tp.target_usd(_basket([4.0, 8.0], tier='tier2')) == 2.00


def test_tier1_layer1_closes_at_half_dollar(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _basket([2.0], side='long', tier='tier1')  # qty = 100 @ 0.10
    assert not tp.check_basket_tp(b, ENTRY)             # 0 profit
    # +0.006 move on 100 qty = +$0.60 gross, fees tiny → net ≥ $0.50.
    assert tp.check_basket_tp(b, ENTRY + 0.006)


def test_tier2_recovery_requires_two_dollars(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _basket([4.0, 8.0], side='long', tier='tier2')  # qty = 200 + 400 = 600
    # +0.002 move = +$1.20 gross (< $2.00 target) → no close.
    assert not tp.check_basket_tp(b, ENTRY + 0.002)
    # +0.005 move = +$3.00 gross → net above $2.00 → close.
    assert tp.check_basket_tp(b, ENTRY + 0.005)


def test_unknown_tier_falls_back_to_tier1(settings: Settings):
    tp = TakeProfitManager(settings)
    # A basket whose volatility isn't a known tier id uses Tier 1 targets.
    assert tp.target_usd(_basket([2.0], tier='legacy')) == 0.50


def test_short_side_profit(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _basket([2.0], side='short', tier='tier1')
    assert not tp.check_basket_tp(b, ENTRY)
    assert tp.check_basket_tp(b, ENTRY - 0.006)         # price down → short profit
