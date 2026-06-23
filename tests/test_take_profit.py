"""Tests for the single-entry fixed-% take-profit / stop-loss.

Each position is a single entry (no recovery, no layers). It closes on the FIRST
of: net PnL ≥ tp_margin_pct × margin ('tp') or net PnL ≤ −sl_margin_pct × margin
('sl'). Sizing uses a realistic low-priced coin (entry $0.10, margin × leverage /
price) at the approved 10× leverage.
"""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from grid.take_profit import TakeProfitManager

ENTRY = 0.10
LEVERAGE = 10


def _qty(margin: float) -> float:
    return (margin * LEVERAGE) / ENTRY


def _pos(margin: float, side='long', tier='tier1') -> Basket:
    """A single-entry position with the given margin (one layer)."""
    b = Basket(symbol='SOL/USDT:USDT', side=side, atr_at_entry=0.001, volatility=tier)
    b.add_layer(RecoveryLayer(1, entry_price=ENTRY, margin=margin,
                              quantity=_qty(margin), side=side))
    return b


def test_tp_sl_targets_scale_with_margin(settings: Settings):
    tp = TakeProfitManager(settings)
    # Tier 1: margin $0.8 → TP 25% = $0.20, SL 12% = $0.096.
    b1 = _pos(0.8, tier='tier1')
    assert abs(tp.tp_target_usd(b1) - 0.20) < 1e-9
    assert abs(tp.sl_target_usd(b1) - 0.096) < 1e-9
    # Tier 2: margin $1.5 → TP $0.375, SL $0.18.
    b2 = _pos(1.5, tier='tier2')
    assert abs(tp.tp_target_usd(b2) - 0.375) < 1e-9
    assert abs(tp.sl_target_usd(b2) - 0.18) < 1e-9


def test_take_profit_fires_on_net_target(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _pos(0.8)                                    # margin 0.8, qty 80
    # Small move → net ≈ $0.07 < $0.20 → hold.
    assert tp.evaluate_exit(b, ENTRY + 0.0010)[0] is None
    assert not tp.check_take_profit(b, ENTRY + 0.0010)
    # +0.0035 → net ≈ $0.27 ≥ $0.20 → take profit.
    reason, m = tp.evaluate_exit(b, ENTRY + 0.0035)
    assert reason == 'tp'
    assert m['net_pnl'] >= m['tp_target'] > 0
    assert tp.check_take_profit(b, ENTRY + 0.0035)


def test_stop_loss_fires_on_net_floor(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _pos(0.8)
    # Small adverse move → net ≈ −$0.05 above the −$0.096 floor → hold.
    assert tp.evaluate_exit(b, ENTRY - 0.0005)[0] is None
    assert not tp.check_stop_loss(b, ENTRY - 0.0005)
    # −0.0015 → net ≈ −$0.13 ≤ −$0.096 → stop loss.
    reason, m = tp.evaluate_exit(b, ENTRY - 0.0015)
    assert reason == 'sl'
    assert m['net_pnl'] <= -m['sl_target']
    assert tp.check_stop_loss(b, ENTRY - 0.0015)


def test_short_side(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _pos(0.8, side='short')
    assert tp.evaluate_exit(b, ENTRY - 0.0035)[0] == 'tp'   # price down → short profit
    assert tp.evaluate_exit(b, ENTRY + 0.0015)[0] == 'sl'   # price up → short loss


def test_tier2_targets(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _pos(1.5, tier='tier2')                     # margin 1.5, qty 150
    assert tp.evaluate_exit(b, ENTRY + 0.0010)[0] is None   # net < $0.375
    assert tp.evaluate_exit(b, ENTRY + 0.0040)[0] == 'tp'   # net ≥ $0.375


def test_unknown_tier_still_uses_global_pcts(settings: Settings):
    # TP/SL are global percentages of margin — no tier lookup needed.
    tp = TakeProfitManager(settings)
    b = _pos(0.8, tier='legacy')
    assert abs(tp.tp_target_usd(b) - 0.20) < 1e-9
    assert abs(tp.sl_target_usd(b) - 0.096) < 1e-9


def test_evaluate_exit_metrics_are_consistent(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _pos(0.8)
    reason, m = tp.evaluate_exit(b, ENTRY + 0.0035)
    assert abs(m['net_pnl'] - (m['gross_pnl'] - m['fee'])) < 1e-9
    assert abs(m['roi'] - m['net_pnl'] / m['total_margin']) < 1e-9
    assert m['decision'] == reason == 'tp'
    assert m['fee'] > 0                              # round-trip fee deducted
    assert m['tp_target'] > 0 and m['sl_target'] > 0
    # A flat (zero-profit) position holds with a clean 'hold' decision.
    _, m0 = tp.evaluate_exit(b, ENTRY)
    assert m0['decision'] == 'hold'


def test_empty_position_holds(settings: Settings):
    tp = TakeProfitManager(settings)
    b = Basket(symbol='SOL/USDT:USDT', side='long', atr_at_entry=0.001, volatility='tier1')
    reason, m = tp.evaluate_exit(b, ENTRY)
    assert reason is None
    assert m['decision'] == 'hold'
