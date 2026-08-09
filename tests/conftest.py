"""Shared pytest fixtures for the Zentry market-analysis tests.

These tests exercise the analysis logic in isolation — no network, no exchange,
no database server.
"""

import os
import sys

# Ensure the project root is importable when running pytest from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from analysis.cache import CANDLE_CACHE
from config.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Settings loaded from the real config/config.json."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Settings.load(os.path.join(root, 'config', 'config.json'))


@pytest.fixture(autouse=True)
def clean_candle_cache():
    """Isolate every test from the process-wide candle cache.

    Without this, two tests using the same provider name and symbol would share
    cached candles — so a test that deliberately supplies a short history would
    silently receive another test's full history instead.
    """
    CANDLE_CACHE.clear()
    yield
    CANDLE_CACHE.clear()
