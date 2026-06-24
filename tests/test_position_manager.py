"""Integration tests for single-entry position management.

Lightweight fakes stand in for the exchange and DB — no network, no database.
These lock down: tier-based sizing, one position per symbol, per-tier position
caps, the take-profit/stop-loss exits, the signal-quality gate, and account
death protection. There is NO recovery, NO Layer 2, NO averaging down.
"""

from config.settings import Settings
from core.dto import Signal
from grid.position_manager import PositionManager
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager

PRICE = 0.10
SYMBOL = 'SOL/USDT:USDT'


class FakeExchange:
    def __init__(self, price=PRICE, filled_ratio=1.0):
        self.price = price
        self.filled_ratio = filled_ratio   # 1.0 = full fill, 0.5 = half, 0.0 = none
        self.orders = []

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
        return {'average': self.price, 'amount': qty, 'filled': qty * self.filled_ratio}

    def close_position(self, symbol, side, qty):
        self.orders.append((symbol, 'close', qty))
        return {'average': self.price, 'filled': qty}

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


def _pm(settings: Settings, balance: float, filled_ratio: float = 1.0):
    ex = FakeExchange(filled_ratio=filled_ratio)
    db = FakeDB()
    rm = RiskManager(settings, db)
    rm.initialize(balance)
    pm = PositionManager(
        exchange_client=ex, settings=settings, database=db, risk_manager=rm,
        position_sizer=PositionSizer(settings), tp_manager=TakeProfitManager(settings),
    )
    return pm, ex, db


def _signal(side='long', strength_score=4, symbol=SYMBOL, atr=0.001) -> Signal:
    return Signal(
        symbol=symbol, side=side, strength=0.8, atr=atr, market_regime='neutral',
        volatility='normal', current_price=PRICE, ema200=PRICE, rsi=25.0,
        bb_lower=PRICE, bb_upper=PRICE + 0.01, reason='test entry',
        strength_score=strength_score,
    )


# ── Tier sizing + single entry ──

def test_below_min_balance_blocks_entry(settings: Settings):
    pm, ex, db = _pm(settings, balance=15.0)
    assert pm.open_position(_signal(), balance=15.0) is None
    assert ex.orders == []
    assert db.baskets == []


def test_tier1_open_uses_point_eight_margin(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket is not None
    assert basket.volatility == 'tier1'             # tier locked onto the position
    assert basket.layer_count == 1                  # SINGLE ENTRY — one layer only
    assert basket.layers[0].margin == 0.8           # Tier 1 margin $0.8
    # 0.8 × 10x / 0.10 = qty 80.
    assert basket.layers[0].quantity == 80.0


def test_tier2_open_uses_one_point_five_margin(settings: Settings):
    pm, ex, db = _pm(settings, balance=50.0)
    basket = pm.open_position(_signal(), balance=50.0)
    assert basket is not None
    assert basket.volatility == 'tier2'
    assert basket.layer_count == 1
    assert basket.layers[0].margin == 1.5           # Tier 2 margin $1.5


def test_partial_fill_sizes_position_to_actual(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0, filled_ratio=0.5)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket is not None
    # Intended qty 80; half fills → qty 40, margin = 40 × 0.10 / 10 = $0.40.
    assert basket.layers[0].quantity == 40.0
    assert basket.layers[0].margin == 0.4


def test_zero_fill_rejects_position(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0, filled_ratio=0.0)
    assert pm.open_position(_signal(), balance=25.0) is None
    assert db.baskets == []


def test_one_position_per_symbol(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    assert pm.open_position(_signal(symbol=SYMBOL), balance=25.0) is not None
    # A second position on the SAME symbol is rejected.
    assert pm.open_position(_signal(symbol=SYMBOL), balance=25.0) is None
    assert len([b for b in db.baskets if b.status == 'active']) == 1


# ── Signal quality gate (replaces the recovery correlation gate) ──

def test_low_signal_score_blocks_entry(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    assert pm.open_position(_signal(strength_score=0), balance=25.0) is None   # below min 1
    assert db.baskets == []
    assert pm.open_position(_signal(strength_score=1), balance=25.0) is not None


# ── Per-tier position limits (single entry → max_positions == max_active_symbols) ──

def test_tier1_caps_at_eight_positions(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)          # Tier 1: max 8
    syms = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT', 'DOGE/USDT:USDT',
            'ADA/USDT:USDT', 'AVAX/USDT:USDT', 'LINK/USDT:USDT', 'DOT/USDT:USDT']
    for sym in syms:
        assert pm.open_position(_signal(symbol=sym), balance=25.0) is not None
    assert len([b for b in db.baskets if b.status == 'active']) == 8
    # Ninth symbol is rejected by the tier's max_active_symbols (8).
    assert pm.open_position(_signal(symbol='TRX/USDT:USDT'), balance=25.0) is None


def test_tier2_caps_at_ten_positions(settings: Settings):
    pm, ex, db = _pm(settings, balance=50.0)          # Tier 2: max 10
    syms = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT', 'DOGE/USDT:USDT',
            'ADA/USDT:USDT', 'AVAX/USDT:USDT', 'LINK/USDT:USDT', 'DOT/USDT:USDT',
            'TRX/USDT:USDT', 'LTC/USDT:USDT']
    for sym in syms:
        assert pm.open_position(_signal(symbol=sym), balance=50.0) is not None
    assert len([b for b in db.baskets if b.status == 'active']) == 10
    assert pm.open_position(_signal(symbol='BCH/USDT:USDT'), balance=50.0) is None


# ── Take-profit / stop-loss exits ──

def test_take_profit_exit_closes_position(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket.layer_count == 1
    ex.price = PRICE + 0.0035                          # net ≈ $0.27 ≥ $0.20 TP
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status != 'active'
    assert db.trades and db.trades[-1].exit_reason == 'tp'


def test_stop_loss_exit_closes_position(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    ex.price = PRICE - 0.0015                          # net ≈ −$0.13 ≤ −$0.096 SL
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status != 'active'
    assert db.trades and db.trades[-1].exit_reason == 'sl'


# ── Symbol-specific cooldown (30 min) after a close ──

def test_symbol_cooldown_blocks_same_symbol_after_close(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    b = pm.open_position(_signal(symbol='SOL/USDT:USDT'), balance=25.0)
    assert b is not None
    # Close SOL via take-profit → starts the 30-min cooldown on SOL.
    ex.price = PRICE + 0.0035
    pm.manage_baskets([b], balance=25.0)
    assert db.trades and db.trades[-1].exit_reason == 'tp'
    assert pm.settings.symbol_cooldown_seconds == 1800
    # SOL is now in cooldown → a new SOL entry is blocked.
    ex.price = PRICE
    assert pm.open_position(_signal(symbol='SOL/USDT:USDT'), balance=25.0) is None
    # A DIFFERENT symbol (XRP) is unaffected — cooldown is symbol-specific.
    assert pm.open_position(_signal(symbol='XRP/USDT:USDT'), balance=25.0) is not None


# ── Account death protection ──

def test_open_blocked_when_balance_below_protection_floor(settings: Settings):
    pm, ex, db = _pm(settings, balance=14.0)
    assert pm.open_position(_signal(), balance=14.0) is None
    assert db.baskets == []


def test_manage_triggers_protection_and_closes_all(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket is not None
    # Price collapse pushes equity below the $15 floor → protection lock + close-all.
    ex.price = 0.06
    pm.manage_baskets([basket], balance=10.0)
    assert pm.risk_manager.is_protection_locked()
    assert all(b.status != 'active' for b in db.baskets)
