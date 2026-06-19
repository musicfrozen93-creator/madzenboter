"""Multi-account isolation + daily-lock persistence regression tests.

Exercises the REAL account-isolation mechanism: ``AccountDatabaseWrapper`` (which
prefixes every state key with ``account_<id>_`` and forces account-scoped trade
queries) combined with a per-account ``RiskManager``. Proves that one account's
daily locks never affect another and that locks persist across a "restart"
(a fresh RiskManager reading the same DB) until the UTC day resets.
"""

from types import SimpleNamespace

from config.settings import Settings
from execution.executor import AccountDatabaseWrapper
from risk.risk_manager import RiskManager


class SharedDB:
    """One shared store standing in for the real Database (all accounts)."""

    def __init__(self):
        self.state = {}                 # account-prefixed keys live here
        self.trades = {}                # {account_id: [trade-like, ...]}

    def get_state(self, key):
        return self.state.get(key)

    def set_state(self, key, value):
        self.state[key] = value

    def get_today_trades(self, account_id=None):
        return list(self.trades.get(account_id, []))

    def save_daily_stats(self, stats, account_id=None):
        pass


def _trade(pnl):
    return SimpleNamespace(pnl=pnl)


def _rm(settings, db, account_id, balance=25.0):
    rm = RiskManager(settings, AccountDatabaseWrapper(db, account_id))
    rm.initialize(balance)
    return rm


def test_loss_lock_is_account_specific(settings: Settings):
    db = SharedDB()
    tier = settings.get_tier(25.0)
    rm_a = _rm(settings, db, account_id=1)
    rm_b = _rm(settings, db, account_id=2)

    # Account A breaches its daily loss limit.
    assert rm_a.check_loss_limit(-5.0, tier) is True
    assert rm_a.can_take_new_entry()[0] is False        # A is LOCKED
    assert rm_b.can_take_new_entry()[0] is True          # B is unaffected

    # Only A's namespaced key was written.
    assert db.state.get('account_1_daily_loss_locked') == 'true'
    assert db.state.get('account_2_daily_loss_locked') != 'true'


def test_profit_lock_is_account_specific(settings: Settings):
    db = SharedDB()
    tier = settings.get_tier(25.0)
    db.trades[1] = [_trade(3.0)]                          # A banked the $3 target
    rm_a = _rm(settings, db, account_id=1)
    rm_b = _rm(settings, db, account_id=2)

    assert rm_a.update_profit_target(0.0, tier) is True
    assert rm_a.can_take_new_entry()[0] is False         # A LOCKED
    assert rm_b.can_take_new_entry()[0] is True           # B ACTIVE


def test_per_account_daily_pnl_is_isolated(settings: Settings):
    db = SharedDB()
    db.trades[1] = [_trade(-2.0)]
    db.trades[2] = [_trade(1.0)]
    rm_a = _rm(settings, db, account_id=1)
    rm_b = _rm(settings, db, account_id=2)
    assert rm_a.daily_realized_pnl() == -2.0
    assert rm_b.daily_realized_pnl() == 1.0


def test_lock_persists_across_restart(settings: Settings):
    db = SharedDB()
    tier = settings.get_tier(25.0)
    rm = _rm(settings, db, account_id=1)
    rm.check_loss_limit(-5.0, tier)                       # lock written to DB

    # "Restart": a brand-new RiskManager over the SAME db, same UTC day.
    rm_restarted = _rm(settings, db, account_id=1)
    assert rm_restarted.can_take_new_entry()[0] is False  # lock survived


def test_lock_clears_on_new_utc_day(settings: Settings):
    db = SharedDB()
    tier = settings.get_tier(25.0)
    rm = _rm(settings, db, account_id=1)
    rm.check_loss_limit(-5.0, tier)
    assert rm.can_take_new_entry()[0] is False

    # Force a stale start-date so the next init rolls over to a new UTC day.
    db.state['account_1_daily_start_date'] = '2000-01-01'
    rm_new_day = _rm(settings, db, account_id=1)
    assert rm_new_day.can_take_new_entry()[0] is True     # locks cleared
    assert db.state.get('account_1_daily_loss_locked') == 'false'
