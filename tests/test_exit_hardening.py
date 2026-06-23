"""Tests for the exit-execution hardening on single-entry positions.

Covers the guarantees layered on top of the take-profit / stop-loss exits:

  • Stop-loss through manage_baskets (reason 'sl')
  • TP lock                 — freeze + guarantee a committed profit exit
  • TP lock persistence     — the lock survives a "restart" (new manager + DB)
  • TP lock retry logic     — exchange rejection holds the lock, next cycle closes
  • Partial-fill closure    — a partially-filled close continues until flat
  • Take-profit exit through manage_baskets (reason 'tp')

Lightweight fakes stand in for the exchange and DB — no network, no database.
There is NO recovery, NO Layer 2, NO averaging down.
"""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer, Signal
from grid.position_manager import PositionManager
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager

PRICE = 0.10
SYMBOL = 'SOL/USDT:USDT'

# ── TakeProfitManager-level sizing helpers (entry $0.10, 10×) ──
TP_ENTRY = 0.10
TP_LEV = 10


def _tp_qty(margin: float) -> float:
    return (margin * TP_LEV) / TP_ENTRY


def _pos(margin: float, side='long', tier='tier1') -> Basket:
    b = Basket(symbol=SYMBOL, side=side, atr_at_entry=0.001, volatility=tier)
    b.add_layer(RecoveryLayer(1, entry_price=TP_ENTRY, margin=margin,
                              quantity=_tp_qty(margin), side=side))
    return b


# ─────────────────────────────────────────────
# Take-profit / stop-loss decisions
# ─────────────────────────────────────────────

def test_stop_loss_fires_on_net_loss(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _pos(0.8, side='long', tier='tier1')           # qty 80, SL target $0.096
    assert tp.evaluate_exit(b, TP_ENTRY - 0.0005)[0] is None   # net ≈ −$0.05 → hold
    reason, m = tp.evaluate_exit(b, TP_ENTRY - 0.0015)         # net ≈ −$0.13 → sl
    assert reason == 'sl'
    assert m['net_pnl'] <= -m['sl_target']
    assert m['decision'] == 'sl'


def test_stop_loss_short_side(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _pos(0.8, side='short', tier='tier1')
    assert tp.evaluate_exit(b, TP_ENTRY + 0.0015)[0] == 'sl'   # price up hurts a short


# ─────────────────────────────────────────────
# Integration fakes (PositionManager)
# ─────────────────────────────────────────────

class FakeExchange:
    def __init__(self, price=PRICE):
        self.price = price
        self.orders = []
        self.close_calls = []
        self.reject_close = False                # raise a non-benign error on close
        self.close_fill_sequence = [1.0]         # per-call fill ratio of the requested qty

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
        if self.reject_close:
            raise Exception('exchange busy: -1001 request timeout')   # non-benign
        idx = min(len(self.close_calls), len(self.close_fill_sequence) - 1)
        ratio = self.close_fill_sequence[idx]
        self.close_calls.append((symbol, side, qty))
        return {'average': self.price, 'filled': qty * ratio}

    def fetch_ticker(self, symbol):
        return {'last': self.price}

    def fetch_positions(self):
        return []


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


def _pm(settings: Settings, db: FakeDB, ex: FakeExchange, balance: float):
    rm = RiskManager(settings, db)
    rm.initialize(balance)
    return PositionManager(
        exchange_client=ex, settings=settings, database=db, risk_manager=rm,
        position_sizer=PositionSizer(settings), tp_manager=TakeProfitManager(settings),
    )


def _signal(side='long', strength_score=4, symbol=SYMBOL, atr=0.001) -> Signal:
    return Signal(
        symbol=symbol, side=side, strength=0.8, atr=atr, market_regime='neutral',
        volatility='normal', current_price=PRICE, ema200=PRICE, rsi=25.0,
        bb_lower=PRICE, bb_upper=PRICE + 0.01, reason='test entry',
        strength_score=strength_score,
    )


TP_PRICE = PRICE + 0.0035   # net ≈ $0.27 ≥ $0.20 Tier-1 TP target


def _tp_lock_key(basket):
    return f'tp_lock_{basket.id}'


# ─────────────────────────────────────────────
# Stop-loss through manage_baskets
# ─────────────────────────────────────────────

def test_manage_closes_position_on_stop_loss(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket.layer_count == 1
    ex.price = PRICE - 0.0015                                   # net ≤ −$0.096
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status != 'active'
    assert db.trades and db.trades[-1].exit_reason == 'sl'


# ─────────────────────────────────────────────
# TP lock: activation, retry, persistence
# ─────────────────────────────────────────────

def test_tp_lock_activates_and_holds_on_rejected_close(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    ex.reject_close = True
    ex.price = TP_PRICE                                         # hits the TP target
    pm.manage_baskets([basket], balance=25.0)
    # Lock persisted with the committed reason; position NOT yet closed.
    assert db.state.get(_tp_lock_key(basket)) == 'tp'
    assert not db.trades


def test_tp_lock_retries_until_closed(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    ex.reject_close = True
    ex.price = TP_PRICE
    pm.manage_baskets([basket], balance=25.0)
    assert db.state.get(_tp_lock_key(basket)) == 'tp'
    assert basket.status != 'closed'

    # Next cycle: exchange recovers, position closes with the ORIGINAL reason.
    basket.status = 'active'
    ex.reject_close = False
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status == 'closed'
    assert db.trades and db.trades[-1].exit_reason == 'tp'
    assert not db.state.get(_tp_lock_key(basket))              # lock released


def test_tp_lock_survives_restart_and_ignores_price_reversal(settings: Settings):
    db, ex1 = FakeDB(), FakeExchange()
    pm1 = _pm(settings, db, ex1, balance=25.0)
    basket = pm1.open_position(_signal(), balance=25.0)
    ex1.reject_close = True
    ex1.price = TP_PRICE                                       # hit TP, but close fails
    pm1.manage_baskets([basket], balance=25.0)
    assert db.state.get(_tp_lock_key(basket)) == 'tp'

    # ── RESTART ── new manager + exchange, same persisted DB state. The position
    # reloads as 'active' and the price has REVERSED below entry (a loss).
    ex2 = FakeExchange(price=PRICE - 0.004)
    pm2 = _pm(settings, db, ex2, balance=25.0)
    reloaded = Basket(
        symbol=basket.symbol, side=basket.side, atr_at_entry=basket.atr_at_entry,
        volatility=basket.volatility, id=basket.id, leverage=basket.leverage,
        account_id=basket.account_id,
    )
    for lr in basket.layers:
        reloaded.add_layer(RecoveryLayer(
            lr.layer_number, lr.entry_price, lr.margin, lr.quantity, lr.side))
    db.baskets = [reloaded]

    pm2.manage_baskets([reloaded], balance=25.0)
    assert reloaded.status == 'closed'                        # closed despite the reversal
    assert db.trades and db.trades[-1].exit_reason == 'tp'
    assert not db.state.get(_tp_lock_key(basket))


# ─────────────────────────────────────────────
# Partial-fill closure
# ─────────────────────────────────────────────

def test_partial_fill_closure_continues_until_flat(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    ex.close_fill_sequence = [0.5, 1.0]                        # half, then the remainder
    ex.price = TP_PRICE
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status == 'closed'
    assert len(ex.close_calls) == 2                            # two close submissions
    assert db.trades and db.trades[-1].exit_reason == 'tp'


def test_partial_close_holds_tp_lock_until_complete(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    ex.close_fill_sequence = [0.5]                            # only ever fills half
    ex.price = TP_PRICE
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status != 'closed'
    assert db.state.get(_tp_lock_key(basket)) == 'tp'         # lock held
    assert not db.trades


# ─────────────────────────────────────────────
# Take-profit exit through manage_baskets
# ─────────────────────────────────────────────

def test_tp_exit_closes_and_releases_lock(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket.layer_count == 1
    ex.price = TP_PRICE
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status == 'closed'
    assert db.trades[-1].exit_reason == 'tp'
    assert not db.state.get(_tp_lock_key(basket))
