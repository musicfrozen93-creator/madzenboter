"""Tests for the reconcile-closure / TP-lock / price-retry hardening fixes.

  • Reconcile full-closure workflow (trade record + exit reason + PnL/ROI)
  • TP-lock release on the reconcile path (no orphaned locks)
  • Trade-record persistence on reconcile
  • Orphan TP-lock detection (the pure core of the startup cleanup)
  • Ticker-fetch retry (no immediate skip of a basket that may be due to close)

Lightweight fakes stand in for the exchange and DB — no network, no database.
"""

import time

from config.settings import Settings
from core.database import find_orphan_tp_locks
from core.dto import Basket, RecoveryLayer, Signal
from grid.position_manager import PositionManager
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager

PRICE = 0.10
SYMBOL = 'XLM/USDT:USDT'


class FakeExchange:
    def __init__(self, price=PRICE):
        self.price = price
        self.orders = []
        self.positions = []          # live positions for fetch_positions()
        self.ticker_fail = 0         # raise on the first N fetch_ticker calls

    def get_symbol_info(self, symbol):
        return {
            'precision': {'amount': 0, 'price': 4},
            'limits': {'amount': {'min': 1}, 'cost': {'min': 5.0}},
        }

    def set_margin_mode(self, *a, **k):
        pass

    def set_leverage(self, *a, **k):
        pass

    def place_market_order(self, symbol, side, qty):
        self.orders.append((symbol, side, qty))
        return {'average': self.price, 'amount': qty, 'filled': qty}

    def close_position(self, symbol, side, qty):
        self.orders.append((symbol, 'close', qty))
        return {'average': self.price, 'filled': qty}

    def fetch_ticker(self, symbol):
        if self.ticker_fail > 0:
            self.ticker_fail -= 1
            raise Exception('ticker timeout -1001')
        return {'last': self.price}

    def fetch_positions(self):
        return list(self.positions)


class FakeDB:
    _account_id = 1

    def __init__(self):
        self.state = {}
        self.baskets = []
        self.trades = []
        self.today_trades = []

    def load_active_baskets(self, account_id=None):
        return [b for b in self.baskets if b.status == 'active']

    def save_basket(self, b):
        self.baskets.append(b)

    def update_basket(self, b):
        pass

    def close_basket(self, basket_id):
        for b in self.baskets:
            if b.id == basket_id:
                b.status = 'closed'

    def save_trade(self, t):
        self.trades.append(t)

    def get_state(self, k):
        return self.state.get(k)

    def set_state(self, k, v):
        self.state[k] = v

    def get_today_trades(self, account_id=None):
        return list(self.today_trades)

    def save_daily_stats(self, s, account_id=None):
        pass


def _pm(settings: Settings, db: FakeDB, ex: FakeExchange, balance: float = 25.0):
    rm = RiskManager(settings, db)
    rm.initialize(balance)
    return PositionManager(
        exchange_client=ex, settings=settings, database=db, risk_manager=rm,
        position_sizer=PositionSizer(settings), recovery_system=RecoverySystem(settings),
        tp_manager=TakeProfitManager(settings),
    )


def _old_basket(symbol=SYMBOL, side='long', margin=2.0, qty=160.0, entry=PRICE) -> Basket:
    # created >60s ago so reconcile does not treat it as freshly opened.
    b = Basket(symbol=symbol, side=side, atr_at_entry=0.001, volatility='tier1',
               leverage=8, account_id=1)
    b.created_at = time.time() - 120
    b.add_layer(RecoveryLayer(1, entry_price=entry, margin=margin, quantity=qty, side=side))
    return b


def _tp_lock_key(basket):
    return f'tp_lock_{basket.id}'


# ─────────────────────────────────────────────
# Reconcile full-closure workflow
# ─────────────────────────────────────────────

def test_reconcile_writes_trade_and_finalizes(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex)
    basket = _old_basket()
    db.baskets.append(basket)
    ex.positions = []                                   # no live position on the exchange

    still_active = pm.reconcile_baskets([basket])
    assert still_active == []                            # basket finalized, not retained
    assert basket.status != 'active'
    # A trade record was persisted with the reconcile exit reason + metadata.
    assert len(db.trades) == 1
    t = db.trades[-1]
    assert t.exit_reason == 'reconciled'
    assert t.basket_id == basket.id
    assert t.exit_time > 0
    assert t.layers_used == 1
    assert t.quantity == 160.0


def test_reconcile_records_final_pnl_and_roi(settings: Settings):
    db, ex = FakeDB(), FakeExchange(price=PRICE + 0.005)   # favourable mark
    pm = _pm(settings, db, ex)
    basket = _old_basket()                                  # entry 0.10, qty 160, margin 2
    db.baskets.append(basket)
    pm.reconcile_baskets([basket])
    t = db.trades[-1]
    # gross = (0.105 − 0.10)*160 = 0.80; fee = 160*0.105*0.0004*2 ≈ 0.01344.
    assert abs(t.pnl - (0.80 - 160 * 0.105 * settings.taker_fee_pct * 2)) < 1e-6
    assert t.margin == 2.0                                  # final margin persisted


def test_reconcile_releases_tp_lock_and_uses_locked_reason(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex)
    basket = _old_basket()
    db.baskets.append(basket)
    # A TP lock was committed before the position vanished.
    db.set_state(_tp_lock_key(basket), 'roi_l1')
    db.set_state(f'{_tp_lock_key(basket)}_time', str(time.time()))

    pm.reconcile_baskets([basket])
    # The committed reason is preserved on the trade, and the lock is released.
    assert db.trades[-1].exit_reason == 'roi_l1'
    assert not db.get_state(_tp_lock_key(basket))           # orphan lock cleared


def test_reconcile_keeps_basket_with_live_position(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex)
    basket = _old_basket()
    db.baskets.append(basket)
    ex.positions = [{'symbol': SYMBOL, 'side': 'long', 'contracts': 160.0}]
    still_active = pm.reconcile_baskets([basket])
    assert still_active == [basket]                         # live position → not finalized
    assert basket.status == 'active'
    assert db.trades == []


# ─────────────────────────────────────────────
# Orphan TP-lock detection (startup cleanup core)
# ─────────────────────────────────────────────

def test_find_orphan_tp_locks_flags_closed_baskets():
    state = {
        'account_5_tp_lock_AAA': 'roi_l1',          # basket AAA NOT active → orphan
        'account_5_tp_lock_AAA_time': '123',        # companion key ignored
        'account_12_tp_lock_BBB': 'roi_recovery',   # basket BBB active → keep
        'account_7_tp_lock_CCC': '',                # released already → not an orphan
        'account_7_tp_lock_DDD': 'false',           # released already → not an orphan
        'account_1_daily_loss_locked': 'true',      # unrelated lock → ignored
    }
    orphans = find_orphan_tp_locks(state, active_basket_ids={'BBB'})
    keys = {k for k, _ in orphans}
    assert keys == {'account_5_tp_lock_AAA'}
    assert ('account_5_tp_lock_AAA', 'AAA') in orphans


def test_find_orphan_tp_locks_empty_when_all_active():
    state = {'account_3_tp_lock_XYZ': 'basket_tp'}
    assert find_orphan_tp_locks(state, active_basket_ids={'XYZ'}) == []


# ─────────────────────────────────────────────
# Ticker-fetch retry hardening
# ─────────────────────────────────────────────

def test_fetch_price_with_retry_succeeds_after_failures(settings: Settings):
    db, ex = FakeDB(), FakeExchange(price=0.123)
    pm = _pm(settings, db, ex)
    ex.ticker_fail = 2                                      # first two attempts raise
    assert pm._fetch_price_with_retry(SYMBOL, attempts=3) == 0.123


def test_fetch_price_with_retry_returns_none_after_exhaustion(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex)
    ex.ticker_fail = 5                                      # always fails within budget
    assert pm._fetch_price_with_retry(SYMBOL, attempts=3) is None


def test_manage_retries_ticker_instead_of_skipping(settings: Settings):
    # The snapshot fetch fails once; manage_baskets must RETRY (not skip) and then
    # close the basket that is over its ROI target.
    db, ex = FakeDB(), FakeExchange(price=PRICE + 0.003)
    pm = _pm(settings, db, ex)
    basket = Basket(symbol=SYMBOL, side='long', atr_at_entry=0.001, volatility='tier1',
                    leverage=8, account_id=1)
    basket.add_layer(RecoveryLayer(1, entry_price=PRICE, margin=2.0, quantity=160.0, side='long'))
    db.baskets.append(basket)
    ex.ticker_fail = 1                                      # snapshot fetch fails once
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status == 'closed'
    assert db.trades and db.trades[-1].exit_reason == 'roi_l1'
