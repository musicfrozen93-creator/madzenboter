"""Regression tests for the daily profit trailing lock (CHANGE #3).

  gain 8%  → floor 5%
  gain 10% → floor 8%
  gain 12% → floor 10%
  gain 15% → immediate hard stop

The lock blocks NEW entries for the rest of the UTC day; it is per-account,
persisted, and reset on the daily rollover.
"""

from config.settings import Settings
from risk.risk_manager import RiskManager


class FakeStateDB:
    """In-memory stand-in for the account-isolated DB state store."""

    def __init__(self) -> None:
        self.store: dict = {}

    def get_state(self, key: str):
        return self.store.get(key)

    def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    def save_daily_stats(self, stats: dict, account_id=None) -> None:
        self.store.setdefault('daily_stats', []).append(stats)


def _rm(settings: Settings, db: FakeStateDB, start: float = 100.0) -> RiskManager:
    rm = RiskManager(settings, db)
    rm.initialize(start)
    return rm


def test_floor_arms_at_8_locks_at_5(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db, 100.0)
    # Below 8% → nothing armed.
    assert rm.update_daily_profit_lock(107.0) is False
    assert float(db.get_state('profit_lock_floor') or 0) == 0.0
    # Reach 8% → floor armed at 5%, not locked (gain still above floor).
    assert rm.update_daily_profit_lock(108.0) is False
    assert float(db.get_state('profit_lock_floor')) == 0.05
    # Rises to 9% → stays open.
    assert rm.update_daily_profit_lock(109.0) is False
    # Falls back to 5% → locked.
    assert rm.update_daily_profit_lock(105.0) is True
    assert db.get_state('profit_lock_triggered') == 'true'
    assert db.get_state('profit_lock_reason') == 'floor'


def test_floor_ratchets_up(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db, 100.0)
    rm.update_daily_profit_lock(110.0)            # 10% → floor 8%
    assert float(db.get_state('profit_lock_floor')) == 0.08
    rm.update_daily_profit_lock(112.0)            # 12% → floor 10%
    assert float(db.get_state('profit_lock_floor')) == 0.10
    # Falls to 9% (< 10% floor) → locked.
    assert rm.update_daily_profit_lock(109.0) is True


def test_floor_never_ratchets_down(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db, 100.0)
    rm.update_daily_profit_lock(112.0)            # floor 10%
    # A subsequent reading at a lower (but still > floor) gain must not lower it.
    rm.update_daily_profit_lock(111.0)            # 11% — still above floor
    assert float(db.get_state('profit_lock_floor')) == 0.10


def test_hard_stop_at_15(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db, 100.0)
    assert rm.update_daily_profit_lock(115.0) is True
    assert db.get_state('profit_lock_triggered') == 'true'
    assert db.get_state('profit_lock_reason') == 'hard_stop'


def test_lock_is_sticky_until_daily_reset(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db, 100.0)
    rm.update_daily_profit_lock(115.0)            # hard stop
    # Even if balance recovers, the lock stays for the rest of the day.
    assert rm.update_daily_profit_lock(120.0) is True
    assert rm.update_daily_profit_lock(100.0) is True


def test_can_take_new_entry_blocked_when_locked(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db, 100.0)
    rm.update_daily_profit_lock(115.0)
    ok, reason = rm.can_take_new_entry(115.0)
    assert ok is False
    assert 'hard-stop' in reason


def test_lock_resets_on_new_day(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db, 100.0)
    rm.update_daily_profit_lock(115.0)            # locked today
    assert db.get_state('profit_lock_triggered') == 'true'
    # Simulate a UTC day rollover; the next observation resets the lock.
    rm._current_date = '2000-01-01'
    assert rm.update_daily_profit_lock(100.0) is False
    assert db.get_state('profit_lock_triggered') == 'false'


def test_lock_survives_restart(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db, 100.0)
    rm.update_daily_profit_lock(115.0)            # locked, persisted in db
    # A fresh RiskManager on the SAME day + same state store stays locked.
    rm2 = RiskManager(settings, db)
    rm2.initialize(100.0)
    assert rm2.check_daily_profit_lock(100.0) is True


def test_no_lock_below_first_tier(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db, 100.0)
    # Peaks at 7% then drifts down — never arms, never locks.
    for bal in (103.0, 107.0, 104.0, 101.0):
        assert rm.update_daily_profit_lock(bal) is False
    assert db.get_state('profit_lock_triggered') in (None, 'false')
