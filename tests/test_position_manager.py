"""Integration tests for tier selection, tier-locking, and exposure caps.

Lightweight fakes stand in for the exchange and DB — no network, no database.
These lock down the critical guarantee that a basket's tier is fixed at open and
never resized by later balance changes (e.g. a deposit crossing a tier boundary).
"""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer, Signal
from grid.position_manager import PositionManager
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager

PRICE = 0.10
SYMBOL = 'TRX/USDT:USDT'


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
        # Fill at the current price; report the ACTUAL filled quantity.
        return {'average': self.price, 'amount': qty, 'filled': qty * self.filled_ratio}

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
        position_sizer=PositionSizer(settings), recovery_system=RecoverySystem(settings),
        tp_manager=TakeProfitManager(settings),
    )
    return pm, ex, db


def _signal(side='long') -> Signal:
    return Signal(
        symbol=SYMBOL, side=side, strength=0.8, atr=0.001, market_regime='neutral',
        volatility='normal', current_price=PRICE, ema200=PRICE, rsi=25.0,
        bb_lower=PRICE, bb_upper=PRICE + 0.01, reason='test entry',
    )


def test_below_min_balance_blocks_entry(settings: Settings):
    pm, ex, db = _pm(settings, balance=15.0)
    assert pm.open_position(_signal(), balance=15.0) is None
    assert ex.orders == []          # no order placed
    assert db.baskets == []


def test_tier1_open_uses_two_dollar_margin(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket is not None
    assert basket.volatility == 'tier1'             # tier locked onto the basket
    assert basket.layers[0].margin == 2.0           # Tier 1 L1 = $2


def test_tier2_open_uses_four_dollar_margin(settings: Settings):
    pm, ex, db = _pm(settings, balance=50.0)
    basket = pm.open_position(_signal(), balance=50.0)
    assert basket is not None
    assert basket.volatility == 'tier2'
    assert basket.layers[0].margin == 4.0           # Tier 2 L1 = $4


def test_recovery_uses_locked_tier_not_current_balance(settings: Settings):
    # Basket opened at Tier 1. Even after a "deposit" to Tier-2 territory, the
    # recovery layer must use the LOCKED Tier-1 margin ($4), never Tier 2 ($8).
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    ex.orders.clear()
    pm._add_recovery_layer(basket, current_price=PRICE)
    assert basket.layer_count == 2
    assert basket.layers[1].margin == 4.0           # Tier 1 L2, NOT Tier 2's $8
    # Total basket exposure stays at the Tier-1 cap ($6).
    assert basket.total_margin == 6.0


def test_recovery_blocked_by_exposure_cap(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    # Contrived Tier-1 basket already near the cap: L1 margin $3 → +$4 = $7 > $6.
    basket = Basket(symbol=SYMBOL, side='long', atr_at_entry=0.001, volatility='tier1',
                    leverage=5, account_id=1)
    basket.add_layer(RecoveryLayer(1, entry_price=PRICE, margin=3.0, quantity=150.0, side='long'))
    ex.orders.clear()
    pm._add_recovery_layer(basket, current_price=PRICE)
    assert basket.layer_count == 1                  # blocked — no Layer 2 added
    assert ex.orders == []


def test_no_third_layer(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    pm._add_recovery_layer(basket, current_price=PRICE)          # Layer 2
    ex.orders.clear()
    pm._add_recovery_layer(basket, current_price=PRICE)          # would be Layer 3
    assert basket.layer_count == 2
    assert ex.orders == []


def test_partial_fill_sizes_basket_to_actual(settings: Settings):
    # Binance fills only half — the basket must track the ACTUAL filled qty/margin.
    pm, ex, db = _pm(settings, balance=25.0, filled_ratio=0.5)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket is not None
    # Tier 1 L1 intends qty 100 @ 0.10 ($2 margin); half fills → qty 50, $1 margin.
    assert basket.layers[0].quantity == 50.0
    assert basket.layers[0].margin == 1.0           # 50 × 0.10 / 5 = $1 actual


def test_zero_fill_rejects_basket(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0, filled_ratio=0.0)
    assert pm.open_position(_signal(), balance=25.0) is None
    assert db.baskets == []                          # nothing persisted on no-fill
