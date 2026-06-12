"""ZenGrid V2 — audit-fix verification suite (findings C1–C5).

Each test reproduces the exact failure scenario from the independent audit
through the PRODUCTION flow: baskets are re-hydrated as fresh DTOs every
management loop with only persisted fields round-tripping (mirroring
core/database.py), so any exit logic that relies on non-persisted state
fails here exactly as it would live.

Run directly:  python tests/test_v2_audit_fixes.py
"""

import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from execution.executor import SignalExecutor
from grid.position_manager import PositionManager
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from risk.stop_loss import StopLossManager
from signals.signal_engine import SignalEngine


# ─────────────────────────────────────────────
# Test doubles
# ─────────────────────────────────────────────

class FakeExchange:
    """Minimal exchange double for basket-management flows."""

    def __init__(self) -> None:
        self.price = 0.0
        self.closed = []

    def fetch_ticker(self, symbol):
        return {'last': self.price}

    def fetch_ohlcv(self, symbol, timeframe, limit=100):
        raise RuntimeError('no candle data in test')

    def close_position(self, symbol, side, quantity):
        self.closed.append((symbol, side, quantity))

    def place_market_order(self, symbol, side, quantity):
        raise RuntimeError('order placement not exercised in test')

    def get_symbol_info(self, symbol):
        return {
            'limits': {'cost': {'min': 5.0}, 'amount': {'min': 0.0}},
            'precision': {'amount': 3},
        }


class FakeDB:
    """Persists ONLY the fields core/database.py persists — fresh DTOs on
    every load, exactly like the production reload-per-loop flow."""

    def __init__(self) -> None:
        self.rows = {}
        self.trades = []
        self.kv = {}

    def _store(self, b: Basket) -> None:
        self.rows[b.id] = {
            'symbol': b.symbol, 'side': b.side,
            'atr_at_entry': b.atr_at_entry, 'volatility': b.volatility,
            'id': b.id, 'created_at': b.created_at, 'status': b.status,
            'leverage': b.leverage, 'account_id': b.account_id,
            'template': b.template, 'risk_budget': b.risk_budget,
            'wind_down': b.wind_down, 'wind_down_at': b.wind_down_at,
            'peak_roi': b.peak_roi, 'be_armed': b.be_armed,
            'layers': [
                (l.layer_number, l.entry_price, l.margin, l.quantity,
                 l.side, l.timestamp, l.status)
                for l in b.layers
            ],
        }

    save_basket = _store
    update_basket = _store

    def load_active_baskets(self, account_id=None):
        out = []
        for d in self.rows.values():
            if d['status'] != 'active':
                continue
            b = Basket(
                symbol=d['symbol'], side=d['side'],
                atr_at_entry=d['atr_at_entry'], volatility=d['volatility'],
                id=d['id'], created_at=d['created_at'], status=d['status'],
                leverage=d['leverage'], account_id=d['account_id'],
                template=d['template'], risk_budget=d['risk_budget'],
                wind_down=d['wind_down'], wind_down_at=d['wind_down_at'],
                peak_roi=d['peak_roi'], be_armed=d['be_armed'],
            )
            for (n, p, m, q, s, t, st) in d['layers']:
                b.layers.append(RecoveryLayer(n, p, m, q, s, t, st))
            out.append(b)
        return out

    def close_basket(self, basket_id):
        self.rows[basket_id]['status'] = 'closed'

    def save_trade(self, trade):
        self.trades.append(trade)

    def get_state(self, key):
        return self.kv.get(key)

    def set_state(self, key, value):
        self.kv[key] = value

    def get_trades_since(self, timestamp, account_id=None):
        return [t for t in self.trades if t.exit_time >= timestamp]


def build_manager(settings: Settings):
    exchange = FakeExchange()
    db = FakeDB()
    pm = PositionManager(
        exchange_client=exchange,
        settings=settings,
        database=db,
        risk_manager=RiskManager(settings, db),
        position_sizer=PositionSizer(settings),
        recovery_system=RecoverySystem(settings),
        tp_manager=TakeProfitManager(settings),
        sl_manager=StopLossManager(settings),
        signal_engine=SignalEngine(exchange, settings),
    )
    return pm, exchange, db


def run_loop(pm, exchange, db, price, balance=1000.0):
    """One production-equivalent management cycle: reload fresh DTOs."""
    exchange.price = price
    pm.manage_baskets(db.load_active_baskets(), balance, None)


def basket(db, *, side='long', layers, atr, risk_budget, template='core',
           leverage=8):
    b = Basket(symbol='DOGE/USDT', side=side, atr_at_entry=atr,
               volatility='medium', leverage=leverage, template=template,
               risk_budget=risk_budget)
    for (n, price, margin, qty) in layers:
        b.add_layer(RecoveryLayer(n, price, margin, qty, side))
    db.save_basket(b)
    return b.id


# ─────────────────────────────────────────────
# C1 — trailing TP must survive per-loop reload and fire on giveback
# ─────────────────────────────────────────────

def test_c1_trailing_tp_fires_across_reloads(settings):
    pm, ex, db = build_manager(settings)
    bid = basket(db, layers=[(1, 0.10, 10.0, 800.0)], atr=0.01,
                 risk_budget=12.0)

    run_loop(pm, ex, db, 0.1030)   # ROI 24% >= 12% target -> arms trailing
    assert db.rows[bid]['status'] == 'active', 'must ride beyond target'
    assert db.rows[bid]['peak_roi'] > 0, 'C1: peak_roi must PERSIST'

    run_loop(pm, ex, db, 0.1031)   # new peak 24.8%
    run_loop(pm, ex, db, 0.1024)   # ROI 19.2% < floor 20.96% -> exit

    assert db.rows[bid]['status'] == 'closed', 'C1: trailing exit must fire'
    assert db.trades[-1].exit_reason == 'basket_tp_trail', db.trades[-1].exit_reason
    print('C1 trailing TP across reloads: PASS')


# ─────────────────────────────────────────────
# C2 — break-even ratchet must survive per-loop reload and fire
# ─────────────────────────────────────────────

def test_c2_be_ratchet_fires_across_reloads(settings):
    pm, ex, db = build_manager(settings)
    bid = basket(db, layers=[(1, 1.00, 12.5, 100.0), (2, 0.99, 12.375, 100.0)],
                 atr=0.01, risk_budget=5.0)

    run_loop(pm, ex, db, 0.99811)  # ROI ~2.50% >= 2% arm threshold
    assert db.rows[bid]['be_armed'] is True, 'C2: be_armed must PERSIST'
    assert db.rows[bid]['status'] == 'active'

    run_loop(pm, ex, db, 0.99537)  # ROI ~0.30% <= 0.5% floor -> exit

    assert db.rows[bid]['status'] == 'closed', 'C2: BE ratchet must fire'
    assert db.trades[-1].exit_reason == 'break_even_exit', db.trades[-1].exit_reason
    print('C2 break-even ratchet across reloads: PASS')


# ─────────────────────────────────────────────
# C3 — legacy baskets (risk_budget == 0) keep exact V1 behaviour
# ─────────────────────────────────────────────

def test_c3_legacy_immediate_basket_tp(settings):
    pm, ex, db = build_manager(settings)
    bid = basket(db, layers=[(1, 0.10, 10.0, 800.0)], atr=0.01,
                 risk_budget=0.0)  # legacy

    run_loop(pm, ex, db, 0.1016)   # ROI 12.8% >= 12% -> V1 immediate TP

    assert db.rows[bid]['status'] == 'closed', 'C3: legacy TP must be immediate'
    assert db.trades[-1].exit_reason == 'basket_tp', db.trades[-1].exit_reason
    print('C3 legacy immediate basket TP (no trailing): PASS')


def test_c3_legacy_ungated_vs_v2_gated_harvesting(settings):
    # Net-NEGATIVE basket with one profitable layer beyond its 2xATR TP:
    # V1/legacy harvests it (ungated); V2 must NOT (basket-health gate).
    layers = [(1, 1.00, 12.5, 100.0), (2, 0.95, 11.875, 100.0)]

    pm, ex, db = build_manager(settings)
    legacy_id = basket(db, layers=layers, atr=0.01, risk_budget=0.0)
    run_loop(pm, ex, db, 0.972)    # net -0.6; L2 profit 2.2 > 2xATR TP
    legacy_l2 = [l for l in db.rows[legacy_id]['layers'] if l[0] == 2][0]
    assert legacy_l2[6] == 'closed', 'C3: legacy individual TP must fire (V1)'

    pm2, ex2, db2 = build_manager(settings)
    v2_id = basket(db2, layers=layers, atr=0.01, risk_budget=5.0)
    run_loop(pm2, ex2, db2, 0.972)
    v2_l2 = [l for l in db2.rows[v2_id]['layers'] if l[0] == 2][0]
    assert v2_l2[6] == 'active', 'V2: harvesting must stay gated on net-positive'
    print('C3 legacy ungated harvesting vs V2 gate: PASS')


# ─────────────────────────────────────────────
# C4 — per-account components cached across loops
# ─────────────────────────────────────────────

def test_c4_component_cache(settings):
    ex = SignalExecutor(db=None, account_manager=None, encryption=None,
                        master_settings=settings)
    builds = []
    ex._build_account_components = lambda acct: builds.append(1) or ('stack',)

    account = SimpleNamespace(id=7, updated_at='2026-06-12T00:00:00',
                              encrypted_api_key='K1', encrypted_api_secret='S1',
                              max_positions=5)
    results = [ex._get_account_components(account) for _ in range(5)]
    assert len(builds) == 1, f'C4: expected 1 build for 5 loops, got {len(builds)}'
    assert all(r is results[0] for r in results), 'must reuse the same stack'

    # Participation-regression fix: updated_at churns every 60s from the
    # sync service (cached_balance/last_sync_at writes) — it must NOT
    # invalidate the cache anymore.
    account.updated_at = '2026-06-12T01:00:00'
    ex._get_account_components(account)
    assert len(builds) == 1, 'sync-driven updated_at churn must NOT rebuild'

    # Trading-relevant changes still invalidate (credential rotation /
    # settings edit).
    account.max_positions = 8
    ex._get_account_components(account)
    assert len(builds) == 2, 'C4: trading-settings change must rebuild'

    account.encrypted_api_key = 'K2'
    ex._get_account_components(account)
    assert len(builds) == 3, 'C4: credential rotation must rebuild'

    ex._component_cache[7]['built_at'] -= 10_000  # TTL expiry
    ex._get_account_components(account)
    assert len(builds) == 4, 'C4: TTL expiry must rebuild'
    print('C4 component cache (reuse / fingerprint / TTL): PASS')


# ─────────────────────────────────────────────
# C5 — debounce counts consecutive evaluations, not wall-clock
# ─────────────────────────────────────────────

def test_c5_debounce_pass_time_independent(settings):
    import signals.signal_engine as se_mod

    clock = SimpleNamespace(t=1_000_000.0)
    real_time = se_mod.time
    se_mod.time = SimpleNamespace(time=lambda: clock.t)
    try:
        eng = SignalEngine(FakeExchange(), settings)

        # Slow watchlist pass (5 min between evaluations) — the old 90s
        # window reset the streak forever; consecutive evals must confirm.
        eng._eval_index['X'] = 1
        assert eng._confirm_signal('X', 'long') is False     # 1/2
        clock.t += 300.0
        eng._eval_index['X'] = 2
        assert eng._confirm_signal('X', 'long') is True, \
            'C5: 5-minute passes must still confirm on the 2nd sighting'

        # A missed evaluation (non-consecutive index) resets the streak.
        eng._eval_index['Y'] = 1
        assert eng._confirm_signal('Y', 'long') is False
        clock.t += 60.0
        eng._eval_index['Y'] = 3                              # skipped pass
        assert eng._confirm_signal('Y', 'long') is False, \
            'non-consecutive evaluation must reset'

        # Watchlist churn: consecutive index but hours apart -> stale, reset.
        eng._eval_index['Z'] = 1
        assert eng._confirm_signal('Z', 'short') is False
        clock.t += 7200.0
        eng._eval_index['Z'] = 2
        assert eng._confirm_signal('Z', 'short') is False, \
            'stale gap beyond ceiling must reset'
        print('C5 evaluation-indexed debounce: PASS')
    finally:
        se_mod.time = real_time


if __name__ == '__main__':
    cfg = Settings.load(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'config', 'config.json',
    ))
    test_c1_trailing_tp_fires_across_reloads(cfg)
    test_c2_be_ratchet_fires_across_reloads(cfg)
    test_c3_legacy_immediate_basket_tp(cfg)
    test_c3_legacy_ungated_vs_v2_gated_harvesting(cfg)
    test_c4_component_cache(cfg)
    test_c5_debounce_pass_time_independent(cfg)
    print()
    print('ALL AUDIT-FIX TESTS PASSED (C1, C2, C3, C4, C5)')
