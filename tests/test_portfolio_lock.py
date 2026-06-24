"""Tests for the per-account portfolio trailing profit lock.

Arms when total open unrealised PnL reaches the tier trigger ($0.50 T1 / $0.80
T2); flattens ALL positions if the aggregate gives back to the floor ($0.35 T1 /
$0.50 T2). Per-account, resets after positions close and on a new UTC day,
independent of (and compatible with) the daily profit lock.
"""

from config.settings import Settings
from core.dto import Signal
from grid.position_manager import PositionManager
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager

PRICE = 0.10


def _rm(settings: Settings, fake_db, balance: float = 25.0) -> RiskManager:
    rm = RiskManager(settings, fake_db)
    rm.initialize(balance)
    return rm


# ─────────────────────────────────────────────
# Tier-threshold unit tests (RiskManager)
# ─────────────────────────────────────────────

def test_tier1_arms_at_trigger(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    t1 = settings.get_tier(25.0)
    assert rm.update_portfolio_profit_lock(0.40, t1) is False     # below $0.50 trigger
    assert not rm.is_portfolio_profit_locked()
    assert rm.update_portfolio_profit_lock(0.50, t1) is False     # arms (never closes on arm)
    assert rm.is_portfolio_profit_locked()
    assert float(fake_db.get_state('peak_portfolio_profit')) == 0.50


def test_tier1_fires_on_giveback(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    t1 = settings.get_tier(25.0)
    assert rm.update_portfolio_profit_lock(0.60, t1) is False      # arm
    assert rm.update_portfolio_profit_lock(0.70, t1) is False      # trail peak up
    assert float(fake_db.get_state('peak_portfolio_profit')) == 0.70
    assert rm.update_portfolio_profit_lock(0.45, t1) is False      # 0.45 > $0.35 floor → hold
    assert rm.update_portfolio_profit_lock(0.35, t1) is True       # ≤ $0.35 floor → FIRE


def test_tier2_thresholds(settings: Settings, fake_db):
    rm = _rm(settings, fake_db, balance=50.0)
    t2 = settings.get_tier(50.0)
    assert rm.update_portfolio_profit_lock(0.79, t2) is False      # below $0.80 trigger
    assert not rm.is_portfolio_profit_locked()
    assert rm.update_portfolio_profit_lock(0.80, t2) is False      # arm
    assert rm.update_portfolio_profit_lock(0.60, t2) is False      # 0.60 > $0.50 floor → hold
    assert rm.update_portfolio_profit_lock(0.50, t2) is True       # ≤ $0.50 floor → FIRE


def test_reset_clears_lock_and_peak(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    t1 = settings.get_tier(25.0)
    rm.update_portfolio_profit_lock(0.60, t1)
    assert rm.is_portfolio_profit_locked()
    rm.reset_portfolio_profit_lock()
    assert not rm.is_portfolio_profit_locked()
    assert not fake_db.get_state('peak_portfolio_profit')


def test_resets_on_new_utc_day(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    t1 = settings.get_tier(25.0)
    rm.update_portfolio_profit_lock(0.60, t1)
    assert rm.is_portfolio_profit_locked()
    rm._current_date = '2000-01-01'          # force a UTC-day rollover
    rm._check_daily_reset(25.0)
    assert not rm.is_portfolio_profit_locked()


def test_independent_of_daily_profit_lock(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    t1 = settings.get_tier(25.0)
    rm.update_portfolio_profit_lock(0.60, t1)        # portfolio armed
    assert rm.is_portfolio_profit_locked()
    assert not rm.is_daily_profit_locked()           # daily profit lock untouched
    allowed, _ = rm.can_take_new_entry()             # portfolio lock never blocks entries
    assert allowed


# ─────────────────────────────────────────────
# Integration (manage_baskets close-all on give-back)
# ─────────────────────────────────────────────

class FakeExchange:
    def __init__(self, price=PRICE):
        self.price = price
        self.orders = []

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
        return {'average': self.price, 'filled': qty}

    def fetch_ticker(self, symbol):
        return {'last': self.price}

    def fetch_positions(self):
        return []


class FakeDB:
    _account_id = 1

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


def _pm(settings: Settings, db: FakeDB, ex: FakeExchange, balance: float):
    rm = RiskManager(settings, db)
    rm.initialize(balance)
    return PositionManager(
        exchange_client=ex, settings=settings, database=db, risk_manager=rm,
        position_sizer=PositionSizer(settings), tp_manager=TakeProfitManager(settings),
    )


def _signal(symbol, side='long') -> Signal:
    return Signal(
        symbol=symbol, side=side, strength=0.8, atr=0.001, market_regime='neutral',
        volatility='normal', current_price=PRICE, ema200=PRICE, rsi=25.0,
        bb_lower=PRICE, bb_upper=PRICE + 0.01, reason='test', strength_score=4,
    )


def test_portfolio_lock_flattens_all_on_giveback(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    syms = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT', 'DOGE/USDT:USDT']
    for s in syms:
        assert pm.open_position(_signal(s), balance=25.0) is not None

    # ARM cycle: 4 positions × gross $0.14 = $0.56 ≥ $0.50 trigger, and each net
    # (~$0.13) is BELOW the $0.16 TP, so no per-position TP fires — positions stay
    # open and the portfolio lock arms.
    ex.price = PRICE + 0.00175
    pm.manage_baskets(db.load_active_baskets(), balance=25.0)
    assert pm.risk_manager.is_portfolio_profit_locked()
    assert len([b for b in db.baskets if b.status == 'active']) == 4
    assert not db.trades

    # GIVE-BACK cycle: aggregate falls to ~4 × $0.08 = $0.32 ≤ $0.35 floor →
    # flatten ALL positions with reason 'portfolio_profit_lock'.
    ex.price = PRICE + 0.001
    pm.manage_baskets(db.load_active_baskets(), balance=25.0)
    assert all(b.status != 'active' for b in db.baskets)
    assert len(db.trades) == 4
    assert all(t.exit_reason == 'portfolio_profit_lock' for t in db.trades)
    # Lock resets once positions are flat.
    assert not pm.risk_manager.is_portfolio_profit_locked()


def test_portfolio_lock_does_not_fire_above_floor(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    for s in ('SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT', 'DOGE/USDT:USDT'):
        pm.open_position(_signal(s), balance=25.0)
    ex.price = PRICE + 0.00175                          # arm (~$0.56)
    pm.manage_baskets(db.load_active_baskets(), balance=25.0)
    assert pm.risk_manager.is_portfolio_profit_locked()
    # Climbs further — stays armed, peak trails up, nothing closes.
    ex.price = PRICE + 0.0020
    remaining = pm.manage_baskets(db.load_active_baskets(), balance=25.0)
    assert len(remaining) == 4
    assert not db.trades
