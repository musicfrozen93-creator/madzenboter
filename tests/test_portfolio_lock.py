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


def test_tier1_fires_below_dynamic_protected(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    t1 = settings.get_tier(25.0)
    assert rm.update_portfolio_profit_lock(0.60, t1) is False      # arm, peak 0.60
    assert rm.update_portfolio_profit_lock(0.70, t1) is False      # peak 0.70 → protected 0.49
    assert float(fake_db.get_state('peak_portfolio_profit')) == 0.70
    assert rm.update_portfolio_profit_lock(0.50, t1) is False      # 0.50 ≥ 0.49 → hold
    assert rm.update_portfolio_profit_lock(0.48, t1) is True       # 0.48 < 0.49 → FIRE


def test_tier2_arms_then_protects(settings: Settings, fake_db):
    rm = _rm(settings, fake_db, balance=50.0)
    t2 = settings.get_tier(50.0)
    assert rm.update_portfolio_profit_lock(0.79, t2) is False      # below $0.80 trigger
    assert not rm.is_portfolio_profit_locked()
    assert rm.update_portfolio_profit_lock(0.80, t2) is False      # arm, peak 0.80 → protected 0.56
    assert rm.update_portfolio_profit_lock(0.60, t2) is False      # 0.60 ≥ 0.56 → hold
    assert rm.update_portfolio_profit_lock(0.50, t2) is True       # 0.50 < 0.56 → FIRE


# Spec examples: protected = max(floor, peak × band%).
TIER1_PROTECTED = [(0.50, 0.35), (1.00, 0.75), (1.50, 1.20), (2.00, 1.70)]
TIER2_PROTECTED = [(0.80, 0.56), (2.00, 1.50), (3.00, 2.40), (4.00, 3.40)]


def test_tier1_protected_levels_match_spec(settings: Settings):
    t1 = settings.get_tier(25.0)
    for peak, protected in TIER1_PROTECTED:
        rm = _rm(settings, FakeDB())
        rm.update_portfolio_profit_lock(peak, t1)                  # arm at the peak
        assert rm.is_portfolio_profit_locked()
        assert float(rm.database.get_state('peak_portfolio_profit')) == peak
        assert abs(rm.protected_profit(peak, t1) - protected) < 1e-9
        assert rm.update_portfolio_profit_lock(protected + 0.01, t1) is False   # above → hold
        assert rm.update_portfolio_profit_lock(protected - 0.01, t1) is True    # below → FIRE


def test_tier2_protected_levels_match_spec(settings: Settings):
    t2 = settings.get_tier(50.0)
    for peak, protected in TIER2_PROTECTED:
        rm = _rm(settings, FakeDB())
        rm.update_portfolio_profit_lock(peak, t2)
        assert abs(rm.protected_profit(peak, t2) - protected) < 1e-9
        assert rm.update_portfolio_profit_lock(protected + 0.01, t2) is False
        assert rm.update_portfolio_profit_lock(protected - 0.01, t2) is True


def test_protection_trails_up_and_never_down(settings: Settings):
    # A profit level that was SAFE earlier becomes a STOP after the peak climbs:
    # the protected level ratchets up with the peak and never falls.
    rm = _rm(settings, FakeDB())
    t1 = settings.get_tier(25.0)
    assert rm.update_portfolio_profit_lock(0.50, t1) is False      # arm, peak 0.50, protected 0.35
    assert rm.update_portfolio_profit_lock(0.70, t1) is False      # 0.70 safe (protected 0.49)
    assert rm.update_portfolio_profit_lock(1.00, t1) is False      # peak 1.00 → protected 0.75
    assert float(rm.database.get_state('peak_portfolio_profit')) == 1.00
    assert rm.update_portfolio_profit_lock(0.70, t1) is True       # same 0.70 now < 0.75 → FIRE


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


def test_portfolio_lock_flattens_below_dynamic_protected(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    syms = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT', 'DOGE/USDT:USDT']
    for s in syms:
        assert pm.open_position(_signal(s), balance=25.0) is not None

    # ARM + trail: 4 positions × gross $0.16 = $0.64 (peak); each net (~$0.15) is
    # BELOW the $0.16 TP so no per-position TP fires. Protected level is
    # max($0.35, $0.64 × 70%) = $0.448.
    ex.price = PRICE + 0.002
    pm.manage_baskets(db.load_active_baskets(), balance=25.0)
    assert pm.risk_manager.is_portfolio_profit_locked()
    assert len([b for b in db.baskets if b.status == 'active']) == 4
    assert not db.trades

    # GIVE-BACK to ~4 × $0.10 = $0.40 — ABOVE the old fixed $0.35 floor but BELOW
    # the dynamic protected $0.448 → flatten ALL with 'portfolio_profit_lock'.
    ex.price = PRICE + 0.00125
    pm.manage_baskets(db.load_active_baskets(), balance=25.0)
    assert all(b.status != 'active' for b in db.baskets)
    assert len(db.trades) == 4
    assert all(t.exit_reason == 'portfolio_profit_lock' for t in db.trades)
    # Lock resets once positions are flat.
    assert not pm.risk_manager.is_portfolio_profit_locked()


def test_portfolio_lock_holds_above_protected(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    for s in ('SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT', 'DOGE/USDT:USDT'):
        pm.open_position(_signal(s), balance=25.0)
    ex.price = PRICE + 0.002                            # arm (~$0.64), protected $0.448
    pm.manage_baskets(db.load_active_baskets(), balance=25.0)
    assert pm.risk_manager.is_portfolio_profit_locked()
    # Still well above protected — peak trails up, nothing closes.
    ex.price = PRICE + 0.00205
    remaining = pm.manage_baskets(db.load_active_baskets(), balance=25.0)
    assert len(remaining) == 4
    assert not db.trades
