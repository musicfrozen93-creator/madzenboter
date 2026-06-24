"""Tests for immediate take-profit execution + the TP fast-path logging.

Guarantees that once the TP condition is true the bot, in the SAME management
cycle: logs TP_DETECTED, activates + persists the TP lock, submits the close
(TP_CLOSE_SENT), and finalizes (TP_CLOSE_CONFIRMED) — with no waiting for a later
cycle and no TP re-evaluation. Also covers TP-lock persistence across a restart.
"""

import logging

from config.settings import Settings
from core.dto import Basket, RecoveryLayer, Signal
from grid.position_manager import PositionManager
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager

PRICE = 0.10
SYMBOL = 'SOL/USDT:USDT'
TP_PRICE = PRICE + 0.0035          # net ≈ $0.19 ≥ Tier-1 TP target ($0.16)


class FakeExchange:
    def __init__(self, price=PRICE):
        self.price = price
        self.orders = []
        self.close_calls = []
        self.reject_close = False

    def get_symbol_info(self, symbol):
        return {'precision': {'amount': 0, 'price': 4},
                'limits': {'amount': {'min': 1}, 'cost': {'min': 5.0}}}

    def set_margin_mode(self, *a, **k):
        pass

    def set_leverage(self, *a, **k):
        pass

    def place_market_order(self, symbol, side, qty):
        self.orders.append((symbol, side, qty))
        return {'average': self.price, 'amount': qty, 'filled': qty}

    def close_position(self, symbol, side, qty):
        if self.reject_close:
            raise Exception('exchange busy: -1001 request timeout')
        self.close_calls.append((symbol, side, qty))
        return {'average': self.price, 'filled': qty}

    def fetch_ticker(self, symbol):
        return {'last': self.price}

    def fetch_positions(self):
        return []


class FakeDB:
    _account_id = 7

    def __init__(self):
        self.state, self.baskets, self.trades, self.today_trades = {}, [], [], []

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
        position_sizer=PositionSizer(settings), tp_manager=TakeProfitManager(settings),
    )


def _signal(side='long', symbol=SYMBOL) -> Signal:
    return Signal(
        symbol=symbol, side=side, strength=0.8, atr=0.001, market_regime='neutral',
        volatility='normal', current_price=PRICE, ema200=PRICE, rsi=25.0,
        bb_lower=PRICE, bb_upper=PRICE + 0.01, reason='test', strength_score=4,
    )


def _tp_lock_key(b):
    return f'tp_lock_{b.id}'


# ── Immediate execution ──

def test_tp_executes_in_same_cycle(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex)
    b = pm.open_position(_signal(), 25.0)
    ex.price = TP_PRICE
    remaining = pm.manage_baskets([b], 25.0)
    assert b.status == 'closed'                  # closed in THIS cycle
    assert remaining == []                       # not carried to a later cycle
    assert len(ex.close_calls) == 1              # close submitted immediately
    assert db.trades and db.trades[-1].exit_reason == 'tp'
    assert not db.state.get(_tp_lock_key(b))     # lock released after confirmed close


# ── Fast-path logs ──

def test_tp_fast_path_logs_emitted(settings: Settings, caplog):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex)
    b = pm.open_position(_signal(), 25.0)
    ex.price = TP_PRICE
    with caplog.at_level(logging.INFO, logger='trades'):
        pm.manage_baskets([b], 25.0)
    msgs = ' | '.join(r.getMessage() for r in caplog.records)
    for tag in ('TP_DETECTED', 'TP_LOCK_ACTIVATED', 'TP_CLOSE_SENT', 'TP_CLOSE_CONFIRMED'):
        assert tag in msgs, f'missing {tag} in trade logs'
    # The TP_DETECTED line carries account, symbol, pnl, target, timestamp.
    detected = next(r.getMessage() for r in caplog.records if 'TP_DETECTED' in r.getMessage())
    for field in ('account=', 'symbol=', 'pnl=', 'target=', 'timestamp='):
        assert field in detected


# ── TP lock activation + persistence ──

def test_tp_lock_activated_and_persisted_on_rejected_close(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex)
    b = pm.open_position(_signal(), 25.0)
    ex.reject_close = True                        # every close attempt fails
    ex.price = TP_PRICE
    pm.manage_baskets([b], 25.0)
    assert db.state.get(_tp_lock_key(b)) == 'tp'  # committed + persisted
    assert b.status != 'closed'                   # held for retry, not lost
    assert not db.trades


def test_tp_lock_persists_across_restart_and_ignores_reversal(settings: Settings):
    db, ex1 = FakeDB(), FakeExchange()
    pm1 = _pm(settings, db, ex1)
    b = pm1.open_position(_signal(), 25.0)
    ex1.reject_close = True
    ex1.price = TP_PRICE
    pm1.manage_baskets([b], 25.0)
    assert db.state.get(_tp_lock_key(b)) == 'tp'

    # Restart: new manager + exchange, same DB; price has reversed to a LOSS.
    ex2 = FakeExchange(price=PRICE - 0.004)
    pm2 = _pm(settings, db, ex2)
    reloaded = Basket(symbol=b.symbol, side=b.side, atr_at_entry=b.atr_at_entry,
                      volatility=b.volatility, id=b.id, leverage=b.leverage,
                      account_id=b.account_id)
    for lr in b.layers:
        reloaded.add_layer(RecoveryLayer(lr.layer_number, lr.entry_price, lr.margin,
                                         lr.quantity, lr.side))
    db.baskets = [reloaded]
    pm2.manage_baskets([reloaded], 25.0)
    assert reloaded.status == 'closed'            # closed for profit despite the reversal
    assert db.trades[-1].exit_reason == 'tp'
    assert not db.state.get(_tp_lock_key(b))
