"""Shared pytest fixtures and lightweight fakes for ZenGrid regression tests.

These tests exercise the upgraded trading logic in isolation — no network, no
exchange, no database server. Fakes stand in for the exchange client and the
DB state store where needed.
"""

import os
import sys

# Ensure the project root is importable when running pytest from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from config.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Settings loaded from the real config/config.json (post-upgrade values)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Settings.load(os.path.join(root, 'config', 'config.json'))


class FakeStateDB:
    """In-memory stand-in for the account-isolated DB state store."""

    def __init__(self) -> None:
        self.store: dict = {}
        # Settable list of today's TradeRecord-like objects (objects with .pnl).
        self.today_trades: list = []

    def get_state(self, key: str):
        return self.store.get(key)

    def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    # Methods PositionManager helpers may call during finalize.
    def close_basket(self, basket_id: str) -> None:
        self.store[f'closed_{basket_id}'] = True

    # Called by RiskManager._check_daily_reset on a UTC day rollover.
    def save_daily_stats(self, stats: dict, account_id=None) -> None:
        self.store.setdefault('daily_stats', []).append(stats)

    # Called by RiskManager.daily_realized_pnl().
    def get_today_trades(self, account_id=None) -> list:
        return list(self.today_trades)


@pytest.fixture
def fake_db() -> FakeStateDB:
    return FakeStateDB()
