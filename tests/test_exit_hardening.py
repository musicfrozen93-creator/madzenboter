"""Tests for the risk/exit hardening update.

Covers the additions layered on top of the existing survival-first core (none of
which they weaken):

  • Basket hard stop-loss   — close a basket at NET −$0.50 ('basket_sl')
  • TP lock                 — freeze + guarantee a committed profit exit
  • TP lock persistence     — the lock survives a "restart" (new manager + DB)
  • TP lock retry logic     — exchange rejection holds the lock, next cycle closes
  • Partial-fill closure    — a partially-filled close continues until flat
  • TRX ROI override        — TRX uses 8% L1/recovery ROI; others keep tier values
  • ROI exit / recovery ROI exit through manage_baskets

Lightweight fakes stand in for the exchange and DB — no network, no database.
"""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer, Signal
from grid.position_manager import PositionManager
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager

PRICE = 0.10
SYMBOL = 'XLM/USDT:USDT'   # a non-TRX supported symbol (keeps tier ROI defaults)
TRX = 'TRX/USDT:USDT'

# ── TakeProfitManager-level sizing helpers (entry $0.10, 5x) ──
TP_ENTRY = 0.10
TP_LEV = 5


def _tp_qty(margin: float) -> float:
    return (margin * TP_LEV) / TP_ENTRY


def _tp_basket(margins, symbol=SYMBOL, side='long', tier='tier1') -> Basket:
    b = Basket(symbol=symbol, side=side, atr_at_entry=0.001, volatility=tier)
    for i, margin in enumerate(margins, start=1):
        b.add_layer(RecoveryLayer(i, entry_price=TP_ENTRY, margin=margin,
                                  quantity=_tp_qty(margin), side=side))
    return b


# ─────────────────────────────────────────────
# Basket hard stop-loss
# ─────────────────────────────────────────────

def test_basket_sl_threshold_is_thirty_cents(settings: Settings):
    assert settings.basket_hard_sl_usd == 0.30


def test_basket_sl_fires_on_net_loss(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _tp_basket([2.0], side='long', tier='tier1')   # qty 100 @ 0.10
    # Small adverse move (−$0.20 gross) holds — above the −$0.30 floor.
    assert not tp.check_basket_sl(b, TP_ENTRY - 0.002)
    assert tp.evaluate_exit(b, TP_ENTRY - 0.002)[0] is None
    # −$0.35 gross → net below −$0.30 → basket_sl.
    assert tp.check_basket_sl(b, TP_ENTRY - 0.0035)
    reason, m = tp.evaluate_exit(b, TP_ENTRY - 0.0035)
    assert reason == 'basket_sl'
    assert m['net_pnl'] <= -settings.basket_hard_sl_usd
    assert m['decision'] == 'basket_sl'


def test_basket_sl_applies_to_recovery_baskets(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _tp_basket([2.0, 4.0], side='long', tier='tier1')   # 2-layer recovery basket
    reason, _ = tp.evaluate_exit(b, TP_ENTRY - 0.002)       # qty 300 → −$0.60 gross
    assert reason == 'basket_sl'


def test_basket_sl_short_side(settings: Settings):
    tp = TakeProfitManager(settings)
    b = _tp_basket([2.0], side='short', tier='tier1')
    assert tp.evaluate_exit(b, TP_ENTRY + 0.0055)[0] == 'basket_sl'   # price up hurts a short


# ─────────────────────────────────────────────
# TRX ROI override
# ─────────────────────────────────────────────

def test_trx_roi_override_values(settings: Settings):
    tier1 = settings.get_tier(25.0)
    tier2 = settings.get_tier(50.0)
    # TRX → 8% / 8% regardless of tier.
    assert settings.roi_targets_for(TRX, tier1) == (0.08, 0.08)
    assert settings.roi_targets_for(TRX, tier2) == (0.08, 0.08)
    # Non-TRX symbols keep their tier defaults.
    assert settings.roi_targets_for('XLM/USDT:USDT', tier1) == (0.12, 0.10)
    assert settings.roi_targets_for('ADA/USDT:USDT', tier2) == (0.10, 0.10)


def test_trx_closes_earlier_than_other_symbols(settings: Settings):
    tp = TakeProfitManager(settings)
    trx = _tp_basket([2.0], symbol=TRX, tier='tier1')        # TRX L1 ROI 8%
    other = _tp_basket([2.0], symbol='XLM/USDT:USDT', tier='tier1')  # tier1 L1 ROI 12%
    price = TP_ENTRY + 0.0022                                # ROI ≈ 10.6% (8% < x < 12%)
    assert tp.evaluate_exit(trx, price)[0] == 'roi_l1'       # TRX closes (≥ 8%)
    assert tp.evaluate_exit(other, price)[0] is None         # XLM still open (< 12%)


def test_trx_recovery_roi_override(settings: Settings):
    tp = TakeProfitManager(settings)
    trx = _tp_basket([2.0, 4.0], symbol=TRX, tier='tier1')   # TRX recovery ROI 8%
    # ROI ≈ 8.x% on $6 total margin → closes for TRX (tier1 recovery is 10%).
    reason, m = tp.evaluate_exit(trx, TP_ENTRY + 0.0018)
    assert reason == 'roi_recovery'
    assert 0.08 <= m['roi'] < 0.10


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
        position_sizer=PositionSizer(settings), recovery_system=RecoverySystem(settings),
        tp_manager=TakeProfitManager(settings),
    )


def _signal(side='long', strength_score=4, symbol=SYMBOL, atr=0.001) -> Signal:
    return Signal(
        symbol=symbol, side=side, strength=0.8, atr=atr, market_regime='neutral',
        volatility='normal', current_price=PRICE, ema200=PRICE, rsi=25.0,
        bb_lower=PRICE, bb_upper=PRICE + 0.01, reason='test entry',
        strength_score=strength_score,
    )


# ─────────────────────────────────────────────
# Basket SL through manage_baskets
# ─────────────────────────────────────────────

def test_manage_closes_basket_on_hard_sl(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(atr=1.0), balance=25.0)   # huge ATR → no recovery
    assert basket.layer_count == 1
    ex.price = PRICE - 0.0055                                   # net ≤ −$0.50
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status != 'active'
    assert db.trades and db.trades[-1].exit_reason == 'basket_sl'


# ─────────────────────────────────────────────
# TP lock: activation, retry, persistence
# ─────────────────────────────────────────────

def _tp_lock_key(basket):
    return f'tp_lock_{basket.id}'


def test_tp_lock_activates_and_holds_on_rejected_close(settings: Settings):
    # A profit target is hit but the exchange rejects every close attempt: the TP
    # lock is set and PERSISTED (basket left open, lock held for the next cycle).
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    ex.reject_close = True
    ex.price = PRICE + 0.003                                    # ROI ≥ 12% → roi_l1 target
    pm.manage_baskets([basket], balance=25.0)
    # Lock persisted with the committed reason; basket NOT yet closed.
    assert db.state.get(_tp_lock_key(basket)) == 'roi_l1'
    assert not db.trades


def test_tp_lock_retries_until_closed(settings: Settings):
    # Cycle 1 rejects the close (lock held); cycle 2 succeeds and closes with the
    # ORIGINAL committed reason, then releases the lock.
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    ex.reject_close = True
    ex.price = PRICE + 0.003
    pm.manage_baskets([basket], balance=25.0)
    assert db.state.get(_tp_lock_key(basket)) == 'roi_l1'
    assert basket.status != 'closed'

    # Next cycle: exchange recovers. Simulate the reload (DB still has it active).
    basket.status = 'active'
    ex.reject_close = False
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status == 'closed'
    assert db.trades and db.trades[-1].exit_reason == 'roi_l1'
    assert not db.state.get(_tp_lock_key(basket))              # lock released


def test_tp_lock_survives_restart_and_ignores_price_reversal(settings: Settings):
    # The lock must survive a process restart AND ignore later price changes: even
    # after price reverses to a LOSS, the locked basket is still closed for profit.
    db, ex1 = FakeDB(), FakeExchange()
    pm1 = _pm(settings, db, ex1, balance=25.0)
    basket = pm1.open_position(_signal(), balance=25.0)
    ex1.reject_close = True
    ex1.price = PRICE + 0.003                                  # hit roi_l1, but close fails
    pm1.manage_baskets([basket], balance=25.0)
    assert db.state.get(_tp_lock_key(basket)) == 'roi_l1'

    # ── RESTART ── brand-new manager + exchange, same persisted DB state. The
    # basket reloads as 'active' and the price has REVERSED below entry (a loss).
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
    assert reloaded.status == 'closed'                         # closed despite the reversal
    assert db.trades and db.trades[-1].exit_reason == 'roi_l1'
    assert not db.state.get(_tp_lock_key(basket))


# ─────────────────────────────────────────────
# Partial-fill closure
# ─────────────────────────────────────────────

def test_partial_fill_closure_continues_until_flat(settings: Settings):
    # The first close only fills half; close_basket must keep closing the
    # remaining quantity until the position is flat, then record the trade.
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    ex.close_fill_sequence = [0.5, 1.0]                        # half, then the remainder
    ex.price = PRICE + 0.003                                   # roi_l1 target
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status == 'closed'
    assert len(ex.close_calls) == 2                            # two close submissions
    assert db.trades and db.trades[-1].exit_reason == 'roi_l1'


def test_partial_close_holds_tp_lock_until_complete(settings: Settings):
    # If the remainder never fills within the retry budget, the basket is NOT
    # finalized and the TP lock stays set for the next cycle.
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    ex.close_fill_sequence = [0.5]                            # only ever fills half
    ex.price = PRICE + 0.003
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status != 'closed'
    assert db.state.get(_tp_lock_key(basket)) == 'roi_l1'    # lock held
    assert not db.trades


# ─────────────────────────────────────────────
# ROI exit / recovery ROI exit through manage_baskets
# ─────────────────────────────────────────────

def test_roi_l1_exit_closes_and_releases_lock(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    assert basket.layer_count == 1
    ex.price = PRICE + 0.003                                  # ROI ≈ 15% > 12% (XLM tier1)
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status == 'closed'
    assert db.trades[-1].exit_reason == 'roi_l1'
    assert not db.state.get(_tp_lock_key(basket))


def test_recovery_roi_exit_closes_and_releases_lock(settings: Settings):
    db, ex = FakeDB(), FakeExchange()
    pm = _pm(settings, db, ex, balance=25.0)
    basket = pm.open_position(_signal(), balance=25.0)
    pm._add_recovery_layer(basket, current_price=PRICE)
    assert basket.layer_count == 2
    ex.price = PRICE + 0.002                                  # ROI ≈ 15% > 10% recovery
    pm.manage_baskets([basket], balance=25.0)
    assert basket.status == 'closed'
    assert db.trades[-1].exit_reason == 'roi_recovery'
    assert not db.state.get(_tp_lock_key(basket))
