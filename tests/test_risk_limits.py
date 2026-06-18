"""Tests for the per-account daily profit target and daily loss limit."""

from types import SimpleNamespace

from config.settings import Settings
from risk.risk_manager import RiskManager


def _trade(pnl: float):
    return SimpleNamespace(pnl=pnl)


def _rm(settings: Settings, fake_db, balance: float = 100.0) -> RiskManager:
    rm = RiskManager(settings, fake_db)
    rm.initialize(balance)
    return rm


def test_new_entry_allowed_by_default(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    allowed, _ = rm.can_take_new_entry()
    assert allowed


def test_profit_target_blocks_new_entries(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    fake_db.today_trades = [_trade(3.0), _trade(2.5)]  # realised $5.50 >= $5 target
    assert rm.update_profit_target() is True
    assert rm.is_daily_profit_locked()
    allowed, reason = rm.can_take_new_entry()
    assert not allowed
    assert 'profit target' in reason


def test_profit_target_not_reached(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    fake_db.today_trades = [_trade(2.0)]  # only $2 realised
    assert rm.update_profit_target() is False
    assert rm.can_take_new_entry()[0]


def test_loss_limit_via_unrealized(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    # No realised PnL, but open baskets are down $3.5 (>= $3 loss limit).
    assert rm.check_loss_limit(open_unrealized=-3.5) is True
    assert rm.is_daily_loss_locked()
    allowed, reason = rm.can_take_new_entry()
    assert not allowed
    assert 'loss limit' in reason


def test_loss_limit_combines_realized_and_unrealized(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    fake_db.today_trades = [_trade(-2.0)]  # realised -$2
    assert rm.check_loss_limit(open_unrealized=-1.5) is True  # total -$3.5


def test_loss_limit_not_breached(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    fake_db.today_trades = [_trade(-1.0)]
    assert rm.check_loss_limit(open_unrealized=-1.0) is False  # total -$2 > -$3
    assert rm.can_take_new_entry()[0]


def test_locks_are_latched(settings: Settings, fake_db):
    rm = _rm(settings, fake_db)
    assert rm.check_loss_limit(open_unrealized=-5.0) is True
    # Even if positions recover, the lock stays for the rest of the UTC day.
    fake_db.today_trades = []
    assert rm.check_loss_limit(open_unrealized=0.0) is True
