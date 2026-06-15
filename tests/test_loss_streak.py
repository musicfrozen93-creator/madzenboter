"""Regression tests for the loss-streak pause (CHANGE #4).

After 3 consecutive losing baskets, NEW entries pause for 1 hour. Any winning /
break-even basket resets the streak. The pause auto-expires, is per-account, and
is persisted in the DB state store (survives restart).
"""

import time

from config.settings import Settings
from risk.risk_manager import RiskManager


class FakeStateDB:
    def __init__(self) -> None:
        self.store: dict = {}

    def get_state(self, key: str):
        return self.store.get(key)

    def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    def save_daily_stats(self, stats: dict, account_id=None) -> None:
        self.store.setdefault('daily_stats', []).append(stats)


def _rm(settings: Settings, db: FakeStateDB) -> RiskManager:
    rm = RiskManager(settings, db)
    rm.initialize(100.0)
    return rm


def test_pause_after_three_consecutive_losses(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db)
    assert rm.is_loss_streak_paused() is False
    rm.record_basket_result(-1.0)
    rm.record_basket_result(-0.5)
    assert rm.is_loss_streak_paused() is False        # only 2
    rm.record_basket_result(-2.0)                     # 3rd → pause
    assert rm.is_loss_streak_paused() is True
    assert 0 < rm.loss_streak_pause_remaining() <= settings.loss_streak_pause_seconds


def test_win_resets_streak(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db)
    rm.record_basket_result(-1.0)
    rm.record_basket_result(-1.0)
    rm.record_basket_result(5.0)                      # winner resets the streak
    rm.record_basket_result(-1.0)                     # count back to 1
    assert rm.is_loss_streak_paused() is False


def test_breakeven_resets_streak(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db)
    rm.record_basket_result(-1.0)
    rm.record_basket_result(0.0)                      # break-even counts as non-loss
    rm.record_basket_result(-1.0)
    rm.record_basket_result(-1.0)
    assert rm.is_loss_streak_paused() is False        # streak is only 2 since reset


def test_pause_auto_expires(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db)
    for _ in range(settings.loss_streak_threshold):
        rm.record_basket_result(-1.0)
    assert rm.is_loss_streak_paused() is True
    # Force the pause window into the past → it auto-expires and clears.
    db.set_state('loss_streak_pause_until', str(time.time() - 10))
    assert rm.is_loss_streak_paused() is False
    assert db.get_state('loss_streak_pause_until') == '0'


def test_pause_survives_restart(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db)
    for _ in range(settings.loss_streak_threshold):
        rm.record_basket_result(-1.0)
    assert rm.is_loss_streak_paused() is True
    # Fresh RiskManager bound to the same state store stays paused.
    rm2 = RiskManager(settings, db)
    rm2.initialize(100.0)
    assert rm2.is_loss_streak_paused() is True


def test_can_take_new_entry_blocked_during_pause(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db)
    for _ in range(settings.loss_streak_threshold):
        rm.record_basket_result(-1.0)
    ok, reason = rm.can_take_new_entry(100.0)
    assert ok is False
    assert 'loss-streak' in reason


def test_counter_resets_after_pause_triggers(settings: Settings):
    db = FakeStateDB()
    rm = _rm(settings, db)
    for _ in range(settings.loss_streak_threshold):
        rm.record_basket_result(-1.0)
    # On triggering the pause the counter resets so a fresh streak is required.
    assert rm._loss_streak_count() == 0
