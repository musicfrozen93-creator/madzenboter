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

# ── Service authentication ──────────────────────────────────────────────────
# The analysis API rejects unauthenticated requests (api/security.py). Tests
# configure a key process-wide and the API fixtures present it; the direct
# security tests override it deliberately to prove the rejection paths.
TEST_SERVICE_KEY = 'test-service-key-0123456789abcdef'


@pytest.fixture(autouse=True)
def service_key(monkeypatch):
    """Configure the shared secret for the duration of each test."""
    monkeypatch.setenv('ANALYSIS_API_KEY', TEST_SERVICE_KEY)
    yield TEST_SERVICE_KEY


@pytest.fixture
def auth_headers():
    """Headers a trusted caller service presents."""
    return {'X-Service-Key': TEST_SERVICE_KEY}
