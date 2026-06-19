"""Tests for the per-account, tier-specific daily profit/loss limits.

Daily PnL is realised (closed trades) + unrealised (open baskets) — never wallet
balance — so deposits/withdrawals can never move it. Tier 1 limits are ±$3,
Tier 2 limits are ±$4.
"""

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
    assert rm.can_take_new_entry()[0]


def test_tier1_profit_target_blocks_new_entries(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)            # target $3
    rm = _rm(settings, fake_db, balance=25.0)
    fake_db.today_trades = [_trade(2.0), _trade(1.5)]  # realised $3.50 ≥ $3
    assert rm.update_profit_target(0.0, tier1) is True
    assert rm.is_daily_profit_locked()
    allowed, reason = rm.can_take_new_entry()
    assert not allowed and 'profit target' in reason


def test_profit_target_includes_unrealized(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)            # target $3
    rm = _rm(settings, fake_db, balance=25.0)
    # No realised PnL, but open baskets are up $3.2 → still latches.
    assert rm.update_profit_target(3.2, tier1) is True


def test_tier1_profit_target_not_reached(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)
    rm = _rm(settings, fake_db, balance=25.0)
    fake_db.today_trades = [_trade(1.0)]
    assert rm.update_profit_target(1.5, tier1) is False  # total $2.50 < $3
    assert rm.can_take_new_entry()[0]


def test_tier1_loss_limit_via_unrealized(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)            # limit $3
    rm = _rm(settings, fake_db, balance=25.0)
    assert rm.check_loss_limit(-3.5, tier1) is True
    assert rm.is_daily_loss_locked()
    allowed, reason = rm.can_take_new_entry()
    assert not allowed and 'loss limit' in reason


def test_loss_limit_combines_realized_and_unrealized(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)
    rm = _rm(settings, fake_db, balance=25.0)
    fake_db.today_trades = [_trade(-2.0)]
    assert rm.check_loss_limit(-1.5, tier1) is True       # total -$3.5 ≤ -$3


def test_loss_limit_not_breached(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)
    rm = _rm(settings, fake_db, balance=25.0)
    fake_db.today_trades = [_trade(-1.0)]
    assert rm.check_loss_limit(-1.0, tier1) is False      # total -$2 > -$3
    assert rm.can_take_new_entry()[0]


def test_tier2_uses_four_dollar_limits(settings: Settings, fake_db):
    tier2 = settings.get_tier(100.0)           # ±$4
    rm = _rm(settings, fake_db, balance=100.0)
    assert rm.update_profit_target(3.5, tier2) is False   # < $4 profit target
    assert rm.check_loss_limit(-3.5, tier2) is False      # > -$4 loss limit
    assert rm.check_loss_limit(-4.0, tier2) is True       # hits the $4 limit


def test_locks_are_latched(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)
    rm = _rm(settings, fake_db, balance=25.0)
    assert rm.check_loss_limit(-5.0, tier1) is True
    # Even if positions recover, the lock stays for the rest of the UTC day.
    fake_db.today_trades = []
    assert rm.check_loss_limit(0.0, tier1) is True


def test_daily_pnl_ignores_wallet_balance(settings: Settings, fake_db):
    # Daily PnL is trade-derived; a balance jump (deposit) must not change it.
    tier1 = settings.get_tier(25.0)
    rm = _rm(settings, fake_db, balance=25.0)
    fake_db.today_trades = [_trade(-1.0)]
    assert rm.daily_trading_pnl(-1.0) == -2.0
    # A "deposit" (balance change) is irrelevant — no balance is read here.
    assert rm.daily_trading_pnl(-1.0) == -2.0


# ── Account death protection (PROTECTION_LOCKED) ──

def test_death_protection_latches_below_tier1_floor(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)               # floor $15
    rm = _rm(settings, fake_db, balance=25.0)
    assert rm.check_account_death_protection(14.0, tier1) is True
    assert rm.is_protection_locked()
    allowed, reason = rm.can_take_new_entry()
    assert not allowed and 'PROTECTION_LOCKED' in reason


def test_death_protection_not_triggered_above_floor(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)
    rm = _rm(settings, fake_db, balance=25.0)
    assert rm.check_account_death_protection(16.0, tier1) is False
    assert not rm.is_protection_locked()


def test_death_protection_tier2_floor(settings: Settings, fake_db):
    tier2 = settings.get_tier(50.0)               # floor $30
    rm = _rm(settings, fake_db, balance=50.0)
    assert rm.check_account_death_protection(31.0, tier2) is False
    assert rm.check_account_death_protection(29.0, tier2) is True


def test_protection_lock_survives_utc_reset(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)
    rm = _rm(settings, fake_db, balance=25.0)
    rm.check_account_death_protection(14.0, tier1)
    assert rm.is_protection_locked()
    # Force a new UTC day: daily locks clear, but the PROTECTION lock is PERMANENT.
    fake_db.store['daily_start_date'] = '2000-01-01'
    rm_new_day = _rm(settings, fake_db, balance=25.0)
    assert rm_new_day.is_protection_locked()
    assert rm_new_day.can_take_new_entry()[0] is False


def test_protection_lock_persists_across_restart(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)
    _rm(settings, fake_db, balance=25.0).check_account_death_protection(10.0, tier1)
    # Fresh RiskManager over the same store (a "restart") — lock still set.
    rm2 = _rm(settings, fake_db, balance=25.0)
    assert rm2.is_protection_locked()


def test_admin_can_clear_protection_lock(settings: Settings, fake_db):
    tier1 = settings.get_tier(25.0)
    rm = _rm(settings, fake_db, balance=25.0)
    rm.check_account_death_protection(14.0, tier1)
    assert rm.can_take_new_entry()[0] is False
    rm.clear_protection_lock()                    # ADMIN action
    assert rm.can_take_new_entry()[0] is True
