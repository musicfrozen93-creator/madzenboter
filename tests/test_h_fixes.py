"""ZenGrid V2 — HIGH-severity audit-fix verification (findings H1–H5).

Reuses the production-equivalent harness from test_v2_audit_fixes (fresh
DTOs every loop, persisted fields only) and adds adversarial scenarios for
each HIGH finding.

Run directly:  python tests/test_h_fixes.py
"""

import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import Settings
from core.dto import Basket, RecoveryLayer, Signal
from execution.executor import SignalExecutor
from grid.templates import TradeTemplate
from portfolio.manager import PortfolioManager
from risk.risk_manager import RiskManager

from tests.test_v2_audit_fixes import (
    FakeDB, FakeExchange, build_manager, run_loop,
)


def make_basket(db, *, layers, atr, risk_budget, side='long', leverage=8,
                wind_down=False):
    b = Basket(symbol='DOGE/USDT', side=side, atr_at_entry=atr,
               volatility='medium', leverage=leverage, template='core',
               risk_budget=risk_budget)
    if wind_down:
        b.wind_down = True
        b.wind_down_at = time.time()
    for (n, price, margin, qty) in layers:
        b.add_layer(RecoveryLayer(n, price, margin, qty, side))
    db.save_basket(b)
    return b.id


# ─────────────────────────────────────────────
# H1 — deterministic stop precedence
# ─────────────────────────────────────────────

def test_h1_single_layer_keeps_tight_stop(settings):
    # 1-layer V2 basket: 3xATR stop must bind BEFORE the wide margin stop.
    pm, ex, db = build_manager(settings)
    bid = make_basket(db, layers=[(1, 0.10, 10.0, 800.0)], atr=0.0005,
                      risk_budget=12.0)
    # ind SL at 0.0985 (loss 1.20); margin stop would need loss 2.00 (0.0975)
    run_loop(pm, ex, db, 0.0984)
    assert db.rows[bid]['status'] == 'closed'
    assert db.trades[-1].exit_reason == 'individual_sl', db.trades[-1].exit_reason
    print('H1a single-layer 3xATR stop binds (no more wide-stop regression): PASS')


def test_h1_budget_binds_on_deep_basket(settings):
    pm, ex, db = build_manager(settings)
    bid = make_basket(db, layers=[(1, 1.00, 12.5, 100.0), (2, 0.99, 12.375, 100.0)],
                      atr=0.001, risk_budget=1.0)  # margin stop = 4.975
    run_loop(pm, ex, db, 0.989)  # loss 1.2 >= min(1.0, 4.975)
    assert db.trades[-1].exit_reason == 'risk_budget_sl', db.trades[-1].exit_reason
    print('H1b budget binds when tighter (reason = risk_budget_sl): PASS')


def test_h1_margin_backstop_binds_when_tighter(settings):
    pm, ex, db = build_manager(settings)
    bid = make_basket(db, layers=[(1, 1.00, 12.5, 100.0), (2, 0.99, 12.375, 100.0)],
                      atr=0.001, risk_budget=50.0)  # margin stop 4.975 < budget
    run_loop(pm, ex, db, 0.969)  # loss 5.2; L1 far beyond 3xATR but 2 layers
    assert db.trades[-1].exit_reason == 'basket_sl', db.trades[-1].exit_reason
    # Proves per-layer SL stayed OFF inside the ladder (no collision return).
    print('H1c margin backstop binds when tighter; ladder keeps no layer-SL: PASS')


# ─────────────────────────────────────────────
# H2 — fee-aware break-even / wind-down floors
# ─────────────────────────────────────────────

def test_h2_be_ratchet_floor_covers_fees(settings):
    # Fee floor at 8x = (2*0.0004 + 0.0005) * 8 = 1.04% of margin.
    pm, ex, db = build_manager(settings)
    bid = make_basket(db, layers=[(1, 1.00, 12.5, 100.0), (2, 0.99, 12.375, 100.0)],
                      atr=0.01, risk_budget=5.0)
    run_loop(pm, ex, db, 0.99811)    # roi 2.5% -> arms
    assert db.rows[bid]['be_armed'] is True
    # roi 0.9%: ABOVE the old 0.5% floor (old code held on, designed to lock
    # a net loss later) but BELOW the 1.04% fee floor -> must exit NOW.
    run_loop(pm, ex, db, 0.996119)
    assert db.rows[bid]['status'] == 'closed'
    assert db.trades[-1].exit_reason == 'break_even_exit'
    print('H2a BE ratchet floor is fee-aware (exits at 0.9%% at 8x): PASS')


def test_h2_wind_down_requires_net_positive(settings):
    pm, ex, db = build_manager(settings)
    bid = make_basket(db, layers=[(1, 0.10, 10.0, 800.0)], atr=0.01,
                      risk_budget=12.0, wind_down=True)
    # roi 0.5% gross: old code closed here ("break-even") and locked a net
    # loss after 1.04% round-trip cost -> must now STAY OPEN.
    run_loop(pm, ex, db, 0.1000625)
    assert db.rows[bid]['status'] == 'active', 'fee-negative wind-down must wait'
    # roi 1.5% gross > 1.04% fee floor -> close as wind_down (net positive).
    run_loop(pm, ex, db, 0.1001875)
    assert db.rows[bid]['status'] == 'closed'
    assert db.trades[-1].exit_reason == 'wind_down'
    print('H2b wind-down exits only at net-positive ROI: PASS')


# ─────────────────────────────────────────────
# H3 — demotion counts only genuine stop-outs
# ─────────────────────────────────────────────

def test_h3_housekeeping_losses_do_not_demote(settings):
    class KV:
        def __init__(self): self.kv = {}
        def get_state(self, k): return self.kv.get(k)
        def set_state(self, k, v): self.kv[k] = v

    rm = RiskManager(settings, KV())

    # Three housekeeping losses: previously demoted the side — must not now.
    for reason in ('time_triage', 'wind_down', 'break_even_exit'):
        rm.record_trade_result('long', -0.5, exit_reason=reason)
    assert rm.get_demoted_sides() == set(), 'housekeeping losses must not demote'

    # Genuine stop-outs still demote — and an interleaved housekeeping loss
    # neither counts nor resets the streak.
    rm.record_trade_result('long', -1.0, exit_reason='basket_sl')
    rm.record_trade_result('long', -0.2, exit_reason='wind_down')      # ignored
    rm.record_trade_result('long', -1.0, exit_reason='risk_budget_sl')
    rm.record_trade_result('long', -1.0, exit_reason='individual_sl')
    assert rm.get_demoted_sides() == {'long'}, 'real stop-outs must demote'

    # Any win clears.
    rm.record_trade_result('long', 0.3, exit_reason='time_triage')
    assert rm.get_demoted_sides() == set()
    print('H3 demotion ignores housekeeping, counts stop-outs, win clears: PASS')


# ─────────────────────────────────────────────
# H4 — real correlation clusters
# ─────────────────────────────────────────────

def test_h4_cluster_map_and_cap(settings):
    pm = PortfolioManager(settings)
    expectations = {
        'DOGE/USDT:USDT': 'meme', '1000PEPE/USDT:USDT': 'meme',
        'SOL/USDT:USDT': 'l1', 'ARB/USDT:USDT': 'l2',
        'UNI/USDT:USDT': 'defi', 'LINK/USDT:USDT': 'infra',
        'FET/USDT:USDT': 'ai', 'XRP/USDT:USDT': 'payments',
        'SAND/USDT:USDT': 'gaming', 'BTC/USDT:USDT': 'major',
        'ZZZUNKNOWN/USDT:USDT': 'alt',
    }
    for sym, cluster in expectations.items():
        got = pm._cluster_of(sym)
        assert got == cluster, f'{sym}: expected {cluster}, got {got}'

    # Cap behaviour: 2 CORE long meme baskets -> 3rd meme long demoted,
    # but an l1 long stays CORE (previously EVERYTHING demoted after 2).
    def core_basket(sym):
        b = Basket(symbol=sym, side='long', atr_at_entry=0.001,
                   volatility='medium', leverage=8, template='core',
                   risk_budget=1.0)
        b.add_layer(RecoveryLayer(1, 1.0, 2.0, 16.0, 'long'))
        return b

    active = [core_basket('DOGE/USDT:USDT'), core_basket('1000PEPE/USDT:USDT')]

    def sig(symbol):
        return Signal(symbol=symbol, side='long', strength=.5, atr=.001,
                      market_regime='trending', volatility='medium',
                      current_price=1.0, ema200=1.0, rsi=35)

    t, _ = pm.evaluate(sig('WIF/USDT:USDT'), TradeTemplate.CORE, 2.0, 8, 1.2,
                       active, 10_000.0, None, 0.0)
    assert t == TradeTemplate.SCOUT, 'third meme CORE must demote'
    t, _ = pm.evaluate(sig('SOL/USDT:USDT'), TradeTemplate.CORE, 2.0, 8, 1.2,
                       active, 10_000.0, None, 0.0)
    assert t == TradeTemplate.CORE, 'different cluster must keep CORE'
    print('H4 cluster map granular; cap per-cluster not market-wide: PASS')


# ─────────────────────────────────────────────
# H5 — per-account signal isolation
# ─────────────────────────────────────────────

def test_h5_signal_copied_per_account(settings):
    captured = {}

    class FakeAcctDB:
        def save_execution_log(self, **kwargs): pass

    fake_components = (
        SimpleNamespace(fetch_balance=lambda: {'total': 100.0}),  # exchange
        settings,
        SimpleNamespace(open_position=lambda s, b, ms=None:
                        captured.setdefault('signal', s) and None),
        SimpleNamespace(initialize=lambda b: None),               # risk mgr
    )

    ex = SignalExecutor(db=FakeAcctDB(), account_manager=None,
                        encryption=None, master_settings=settings)
    ex._get_account_components = lambda acct: fake_components

    original = Signal(symbol='DOGE/USDT', side='long', strength=.5, atr=.001,
                      market_regime='trending', volatility='medium',
                      current_price=1.0, ema200=1.0, rsi=35)
    original.alignment_score = 0.0

    account = SimpleNamespace(id=1, label='t', updated_at='x')
    ex._execute_for_account(account, original, signal_id=1, market_state=None)

    got = captured['signal']
    assert got is not original, 'H5: account must receive its OWN copy'
    assert got.symbol == original.symbol and got.side == original.side
    got.alignment_score = 0.99
    assert original.alignment_score == 0.0, \
        'mutating the per-account copy must not touch the shared signal'
    print('H5 per-account signal copy isolates mutation: PASS')


if __name__ == '__main__':
    cfg = Settings.load(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'config', 'config.json',
    ))
    test_h1_single_layer_keeps_tight_stop(cfg)
    test_h1_budget_binds_on_deep_basket(cfg)
    test_h1_margin_backstop_binds_when_tighter(cfg)
    test_h2_be_ratchet_floor_covers_fees(cfg)
    test_h2_wind_down_requires_net_positive(cfg)
    test_h3_housekeeping_losses_do_not_demote(cfg)
    test_h4_cluster_map_and_cap(cfg)
    test_h5_signal_copied_per_account(cfg)
    print()
    print('ALL HIGH-FINDING TESTS PASSED (H1, H2, H3, H4, H5)')
