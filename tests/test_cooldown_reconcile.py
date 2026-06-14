"""Regression tests for same-symbol cooldown (CHANGE #2), reconciliation and
robust close handling (CHANGE #9)."""

import time

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from grid.position_manager import PositionManager


class FakeStateDB:
    def __init__(self):
        self.store = {}
        self.closed = []

    def get_state(self, key):
        return self.store.get(key)

    def set_state(self, key, value):
        self.store[key] = value

    def close_basket(self, basket_id):
        self.closed.append(basket_id)


class FakeExchange:
    def __init__(self, positions):
        self._positions = positions

    def fetch_positions(self):
        return self._positions


def _pm(settings, db, exchange=None) -> PositionManager:
    # Only settings/database/exchange are exercised by the methods under test.
    return PositionManager(
        exchange_client=exchange, settings=settings, database=db,
        risk_manager=None, position_sizer=None, recovery_system=None,
        tp_manager=None, sl_manager=None, signal_engine=None,
    )


def _basket(symbol='SOL/USDT:USDT', side='long', age_seconds=120) -> Basket:
    b = Basket(symbol=symbol, side=side, atr_at_entry=1.0, volatility='medium')
    b.created_at = time.time() - age_seconds
    b.add_layer(RecoveryLayer(1, 100.0, 2.0, 1.0, side))
    return b


def test_cooldown_starts_and_blocks(settings: Settings):
    db = FakeStateDB()
    pm = _pm(settings, db)
    sym = 'SOL/USDT:USDT'
    assert pm._cooldown_remaining(sym) == 0.0
    pm._finalize_closed_state(sym, 'basket-123')
    remaining = pm._cooldown_remaining(sym)
    assert 0 < remaining <= settings.symbol_cooldown_seconds
    # Profit-protection armed flag is cleared on close.
    assert db.get_state(pm._armed_key('basket-123')) == ''


def test_cooldown_expires(settings: Settings):
    db = FakeStateDB()
    pm = _pm(settings, db)
    sym = 'SOL/USDT:USDT'
    # Closed longer ago than the window → no remaining cooldown.
    db.set_state(pm._cooldown_key(sym), str(time.time() - settings.symbol_cooldown_seconds - 10))
    assert pm._cooldown_remaining(sym) == 0.0


def test_cooldown_applies_regardless_of_reason(settings: Settings):
    # _finalize_closed_state is reason-agnostic; any close path that calls it
    # (TP, SL, profit protection, reconcile) starts the same cooldown.
    db = FakeStateDB()
    pm = _pm(settings, db)
    pm._finalize_closed_state('AAA/USDT:USDT', 'b1')
    assert pm._cooldown_remaining('AAA/USDT:USDT') > 0


def test_benign_close_error_detection(settings: Settings):
    pm = _pm(settings, FakeStateDB())
    assert pm._is_benign_close_error(Exception('ReduceOnly Order is rejected')) is True
    assert pm._is_benign_close_error(Exception('position not exist')) is True
    assert pm._is_benign_close_error(Exception('Insufficient balance')) is False


def test_reconcile_closes_stale_basket(settings: Settings):
    db = FakeStateDB()
    # Exchange reports NO open positions → the active DB basket is stale.
    pm = _pm(settings, db, exchange=FakeExchange(positions=[]))
    stale = _basket('SOL/USDT:USDT', 'long', age_seconds=120)
    remaining = pm.reconcile_baskets([stale])
    assert remaining == []
    assert stale.id in db.closed
    assert stale.status == 'closed'
    # Cooldown started for the reconciled symbol.
    assert pm._cooldown_remaining('SOL/USDT:USDT') > 0


def test_reconcile_keeps_live_basket(settings: Settings):
    db = FakeStateDB()
    positions = [{'symbol': 'SOL/USDT:USDT', 'side': 'long', 'contracts': 1.0}]
    pm = _pm(settings, db, exchange=FakeExchange(positions))
    live = _basket('SOL/USDT:USDT', 'long', age_seconds=120)
    remaining = pm.reconcile_baskets([live])
    assert remaining == [live]
    assert live.id not in db.closed


def test_reconcile_skips_fresh_basket(settings: Settings):
    db = FakeStateDB()
    pm = _pm(settings, db, exchange=FakeExchange(positions=[]))
    fresh = _basket('SOL/USDT:USDT', 'long', age_seconds=5)  # < 60s grace
    remaining = pm.reconcile_baskets([fresh])
    assert remaining == [fresh]
    assert fresh.id not in db.closed
