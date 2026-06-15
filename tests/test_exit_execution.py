"""Regression tests for immediate exit execution + safety (CHANGE #6).

Covers:
  • close idempotency — a basket can never be closed twice (no duplicate
    reduce-only close orders), even under a concurrent race.
  • per-account component cache — components (with markets already loaded) are
    reused across loops and rebuilt only when the account changes, removing the
    repeated market-loading that delayed closes.
"""

import threading
import time

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from execution.executor import SignalExecutor
from grid.position_manager import PositionManager


# ─────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────

class FakeDB:
    def __init__(self):
        self.store = {}
        self.closed = []
        self.trades = []

    def get_state(self, key):
        return self.store.get(key)

    def set_state(self, key, value):
        self.store[key] = value

    def close_basket(self, basket_id):
        self.closed.append(basket_id)

    def save_trade(self, trade):
        self.trades.append(trade)


class CountingExchange:
    def __init__(self, delay=0.0):
        self.close_calls = 0
        self._delay = delay
        self._lock = threading.Lock()

    def fetch_ticker(self, symbol):
        return {'last': 100.0}

    def close_position(self, symbol, side, quantity):
        with self._lock:
            self.close_calls += 1
        if self._delay:
            time.sleep(self._delay)
        return {'id': 'order-1', 'average': 100.0}


def _pm(settings, db, exchange) -> PositionManager:
    pm = PositionManager(
        exchange_client=exchange, settings=settings, database=db,
        risk_manager=None, position_sizer=None, recovery_system=None,
        tp_manager=None, sl_manager=None, signal_engine=None,
    )
    return pm


def _basket() -> Basket:
    b = Basket(symbol='SOL/USDT:USDT', side='long', atr_at_entry=1.0, volatility='medium')
    b.add_layer(RecoveryLayer(1, entry_price=100.0, margin=1.0, quantity=1.0, side='long'))
    return b


# ─────────────────────────────────────────────
# Double-close protection
# ─────────────────────────────────────────────

def test_close_basket_is_idempotent(settings: Settings):
    db, ex = FakeDB(), CountingExchange()
    pm = _pm(settings, db, ex)
    b = _basket()

    trade = pm.close_basket(b, 'basket_tp')
    assert trade is not None
    assert ex.close_calls == 1
    assert b.status == 'closed'

    # A second close on the (now closed) basket must NOT submit another order.
    again = pm.close_basket(b, 'basket_sl')
    assert again is None
    assert ex.close_calls == 1


def test_concurrent_close_submits_one_order(settings: Settings):
    db, ex = FakeDB(), CountingExchange(delay=0.05)
    pm = _pm(settings, db, ex)
    b = _basket()

    results = []

    def worker():
        results.append(pm.close_basket(b, 'emergency_sl'))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread closed the basket; the rest short-circuited.
    assert ex.close_calls == 1
    assert sum(1 for r in results if r is not None) == 1
    assert b.status == 'closed'


def test_closing_claim_is_released_after_close(settings: Settings):
    db, ex = FakeDB(), CountingExchange()
    pm = _pm(settings, db, ex)
    b = _basket()
    pm.close_basket(b, 'basket_tp')
    # In-flight set is emptied so it never leaks / blocks future baskets.
    assert b.id not in pm._closing


# ─────────────────────────────────────────────
# Per-account component cache
# ─────────────────────────────────────────────

class FakeAccount:
    def __init__(self, account_id=1, key='k', secret='s', testnet=False,
                 risk=0.02, lev=None, tp=None, sl=None):
        self.id = account_id
        self.encrypted_api_key = key
        self.encrypted_api_secret = secret
        self.use_testnet = testnet
        self.risk_pct = risk
        self.leverage_override = lev
        self.tp_settings = tp
        self.sl_settings = sl


def _executor(settings: Settings) -> SignalExecutor:
    return SignalExecutor(
        db=None, account_manager=None, encryption=None, master_settings=settings,
    )


def test_components_cached_and_reused(settings: Settings):
    ex = _executor(settings)
    calls = {'n': 0}

    def fake_construct(account):
        calls['n'] += 1
        return ('client', settings, 'pm', 'risk')

    ex._construct_account_components = fake_construct
    acct = FakeAccount()

    first = ex._build_account_components(acct)
    second = ex._build_account_components(acct)
    assert first is second                 # same cached tuple reused
    assert calls['n'] == 1                 # built only once (no repeated load)


def test_components_rebuilt_when_account_changes(settings: Settings):
    ex = _executor(settings)
    calls = {'n': 0}
    ex._construct_account_components = lambda account: (calls.__setitem__('n', calls['n'] + 1), ('c', settings, 'pm', 'risk'))[1]

    acct = FakeAccount(lev=10)
    ex._build_account_components(acct)
    assert calls['n'] == 1

    # Rotating a key / changing settings changes the fingerprint → rebuild.
    acct.leverage_override = 5
    ex._build_account_components(acct)
    assert calls['n'] == 2


def test_prune_drops_inactive_accounts(settings: Settings):
    ex = _executor(settings)
    ex._construct_account_components = lambda account: ('c', settings, 'pm', 'risk')
    ex._build_account_components(FakeAccount(account_id=1))
    ex._build_account_components(FakeAccount(account_id=2))
    assert set(ex._component_cache) == {1, 2}
    ex._prune_component_cache({1})
    assert set(ex._component_cache) == {1}
