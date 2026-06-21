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
        position_sizer=PositionSizer(settings), recovery_system=RecoverySystem(settings),
        tp_manager=TakeProfitManager(settings),
    )
    return pm, ex, db


def _signal(side='long', strength_score=4, symbol=SYMBOL, atr=0.001) -> Signal:
    return Signal(
        symbol=symbol, side=side, strength=0.8, atr=atr, market_regime='neutral',
        volatility='normal', current_price=PRICE, ema200=PRICE, rsi=25.0,
        bb_lower=PRICE, bb_upper=PRICE + 0.01, reason='test entry',
        strength_score=strength_score,
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


def test_recovery_allowed_despite_inflated_recorded_margin(settings: Settings):
    # EXPOSURE BUG FIX: a recorded L1 margin that drifted ABOVE intended (e.g. from
    # fill-price divergence) must NOT falsely block the legitimate 2-layer recovery.
    # The cap uses the tier's INTENDED margins (L1 $2 + L2 $4 = $6 ≤ $6), so a
    # basket whose recorded L1 margin reads $3 still gets its recovery layer.
    pm, ex, db = _pm(settings, balance=25.0)
    basket = Basket(symbol=SYMBOL, side='long', atr_at_entry=0.001, volatility='tier1',
                    leverage=8, account_id=1)
    basket.add_layer(RecoveryLayer(1, entry_price=PRICE, margin=3.0, quantity=240.0, side='long'))
    ex.orders.clear()
    pm._add_recovery_layer(basket, current_price=PRICE)
    assert basket.layer_count == 2                  # recovery ALLOWED (fix), not blocked
    assert ex.orders                                # an order was placed


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
    # Tier 1 L1 ($2) at 8x / $0.10 intends qty 160; half fills → qty 80, $1 margin.
    assert basket.layers[0].quantity == 80.0
    assert basket.layers[0].margin == 1.0           # 80 × 0.10 / 8 = $1 actual


def test_zero_fill_rejects_basket(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0, filled_ratio=0.0)
    assert pm.open_position(_signal(), balance=25.0) is None
    assert db.baskets == []                          # nothing persisted on no-fill


# ── Correlation protection (second-symbol rule) ──

def test_first_basket_needs_score_two(settings: Settings):
    pm, ex, db = _pm(settings, balance=50.0)
    assert pm.open_position(_signal(strength_score=1), balance=50.0) is None   # too weak
    assert db.baskets == []
    assert pm.open_position(_signal(strength_score=2), balance=50.0) is not None  # ok


def test_second_correlated_basket_needs_score_three(settings: Settings):
    pm, ex, db = _pm(settings, balance=50.0)        # Tier 2: up to 3 symbols
    assert pm.open_position(_signal(strength_score=2, symbol='TRX/USDT:USDT'), balance=50.0)
    # A second correlated basket with only score 2 is rejected (needs 3).
    assert pm.open_position(_signal(strength_score=2, symbol='XRP/USDT:USDT'), balance=50.0) is None
    # Score 3 is accepted.
    assert pm.open_position(_signal(strength_score=3, symbol='XRP/USDT:USDT'), balance=50.0)


# ── Tier-based position limits ──

def test_tier1_caps_at_two_symbols(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)        # Tier 1: max 2 symbols
    assert pm.open_position(_signal(symbol='TRX/USDT:USDT'), balance=25.0)
    assert pm.open_position(_signal(symbol='XRP/USDT:USDT'), balance=25.0)
    # Third symbol is rejected by the tier's max_active_symbols (2).
    assert pm.open_position(_signal(symbol='XLM/USDT:USDT'), balance=25.0) is None


def test_tier2_allows_three_symbols(settings: Settings):
    pm, ex, db = _pm(settings, balance=50.0)        # Tier 2: max 3 symbols
    assert pm.open_position(_signal(symbol='TRX/USDT:USDT'), balance=50.0)
    assert pm.open_position(_signal(symbol='XRP/USDT:USDT'), balance=50.0)
    assert pm.open_position(_signal(symbol='XLM/USDT:USDT'), balance=50.0)
    assert len([b for b in db.baskets if b.status == 'active']) == 3


# ── Account death protection ──

def test_open_blocked_when_balance_below_protection_floor(settings: Settings):
    # Balance $14 → below the Tier-1 floor ($15): protection lock latches at open.
    pm, ex, db = _pm(settings, balance=14.0)
    assert pm.open_position(_signal(), balance=14.0) is None
    # $14 is also below min_tier_balance ($20), so no tier — either way, no trade.
    assert db.baskets == []


def test_recovery_roi_exit_closes_basket(settings: Settings):
    # Open a Tier-1 basket, add the recovery layer, then a small favourable move
    # crosses the 10% ROI target (~$0.60) and closes the basket via 'roi_recovery'.
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    pm._add_recovery_layer(basket, current_price=PRICE)
    assert basket.layer_count == 2
    ex.price = PRICE + 0.002                              # ROI ≈ 15% > 10%, < $1.50 USD
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status != 'active'
    assert db.trades and db.trades[-1].exit_reason == 'roi_recovery'


def test_layer1_roi_exit_closes_basket(settings: Settings):
    # A Layer-1-only basket closes via 'roi_l1' once it reaches the 12% ROI target
    # (~$0.24 on $2 margin), before the $0.50 USD target — addresses the
    # "profitable trades remained open" report.
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket.layer_count == 1
    ex.price = PRICE + 0.002                              # ROI ≈ 15% > 12%, < $0.50 USD
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status != 'active'
    assert db.trades and db.trades[-1].exit_reason == 'roi_l1'


def test_atr_trigger_adds_recovery_layer(settings: Settings):
    # Recovery still works through manage_baskets via the ATR trigger, which in
    # normal volatility fires at a loss WELL BELOW the −$0.50 basket hard-SL floor
    # (so the basket adds Layer 2 rather than stopping out). ATR 0.001 → distance
    # 0.002 → trigger at 0.098; L1 loss there = (0.10−0.098)*160 = $0.32 (< $0.50).
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(atr=0.001), balance=25.0)
    assert basket.layer_count == 1
    ex.price = 0.098
    pm.manage_baskets([basket], balance=25.0)
    assert basket.layer_count == 2                            # recovery layer added
    assert basket.status == 'active'                          # not SL-stopped


def test_basket_sl_preempts_loss_trigger_recovery(settings: Settings):
    # Survival-first priority: when a Layer-1 loss reaches the −$0.50 hard-SL floor
    # WITHOUT the ATR trigger having fired (large ATR), the basket is stopped out
    # via 'basket_sl' instead of doubling down with a recovery layer. The recovery
    # code path is unchanged (see test_recovery.py); the hard SL simply outranks it.
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(atr=1.0), balance=25.0)  # ATR huge → ATR never triggers
    assert basket.layer_count == 1
    ex.price = 0.0960                                          # L1 loss ≈ $0.64 net → ≤ −$0.50
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status != 'active'
    assert db.trades and db.trades[-1].exit_reason == 'basket_sl'


def test_manage_triggers_protection_and_closes_all(settings: Settings):
    pm, ex, db = _pm(settings, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket is not None
    # Price collapse pushes equity below the $15 floor → protection lock + close-all.
    ex.price = 0.06
    pm.manage_baskets([basket], balance=10.0)
    assert pm.risk_manager.is_protection_locked()
    assert all(b.status != 'active' for b in db.baskets)
