"""Tests for the Twelve Data forex provider and its registry gating.

Network is never touched: `_request` is monkeypatched with a canned Twelve Data
payload so the parsing, symbol translation, and candle contract can be verified
offline. The point is that forex flows through the SAME provider abstraction and
the SAME analysis engine as crypto.
"""

import pandas as pd
import pytest

from providers.base import MarketDataUnavailableError, UnknownSymbolError, validate_candles
from providers.twelvedata import TwelveDataForexProvider


def _provider():
    return TwelveDataForexProvider(settings=None, api_key='test-key')


# Newest-first, exactly as Twelve Data returns it.
CANNED = {
    'status': 'ok',
    'values': [
        {'datetime': '2026-08-09 12:30:00', 'open': '1.1010', 'high': '1.1020', 'low': '1.1005', 'close': '1.1015'},
        {'datetime': '2026-08-09 12:15:00', 'open': '1.1000', 'high': '1.1012', 'low': '1.0998', 'close': '1.1010'},
        {'datetime': '2026-08-09 12:00:00', 'open': '1.0995', 'high': '1.1002', 'low': '1.0990', 'close': '1.1000'},
    ],
}


def test_symbol_translation_to_twelvedata_form():
    assert TwelveDataForexProvider._to_pair('EURUSD') == 'EUR/USD'
    assert TwelveDataForexProvider._to_pair('eur_usd') == 'EUR/USD'
    assert TwelveDataForexProvider._to_pair('XAUUSD') == 'XAU/USD'


def test_unrecognisable_pair_is_rejected():
    with pytest.raises(UnknownSymbolError):
        TwelveDataForexProvider._to_pair('BTC')  # 3 chars, no underscore


def test_market_and_timeframes():
    p = _provider()
    assert p.market == 'forex'
    for tf in ('15m', '1h', '4h', '1d'):
        assert tf in p.timeframes
    assert 'EURUSD' in p.list_symbols()


def test_symbol_precision_heuristics():
    p = _provider()
    assert p.get_symbol_info('EURUSD').price_precision == 5
    assert p.get_symbol_info('USDJPY').price_precision == 3
    assert p.get_symbol_info('XAUUSD').price_precision == 2


def test_forex_has_no_systemic_benchmark():
    assert _provider().reference_symbol() is None


def test_fetch_candles_parses_and_orders_oldest_first(monkeypatch):
    p = _provider()
    monkeypatch.setattr(p, '_request', lambda *a, **k: CANNED)
    df = p.fetch_candles('EURUSD', '15m', limit=3)

    assert list(df.columns) == ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    assert len(df) == 3
    # Reversed to oldest-first.
    assert df['close'].iloc[0] == pytest.approx(1.1000)
    assert df['close'].iloc[-1] == pytest.approx(1.1015)
    # Forex volume defaults to 0 and passes the (hardened) candle contract.
    assert (df['volume'] == 0).all()
    assert df['timestamp'].is_monotonic_increasing


def test_fetch_candles_raises_on_empty_values(monkeypatch):
    p = _provider()
    monkeypatch.setattr(p, '_request', lambda *a, **k: {'status': 'ok', 'values': []})
    with pytest.raises(MarketDataUnavailableError):
        p.fetch_candles('EURUSD', '15m')


def test_quote_derives_from_the_latest_candle(monkeypatch):
    p = _provider()
    monkeypatch.setattr(p, '_request', lambda *a, **k: CANNED)
    q = p.fetch_quote('EURUSD')
    assert q.last == pytest.approx(1.1015)
    assert q.spread == 0.0  # forex spread not fabricated


def test_unsupported_timeframe_is_rejected():
    p = _provider()
    with pytest.raises(Exception):
        p.fetch_candles('EURUSD', '3d')  # not in forex timeframes


def test_initialize_requires_a_key():
    p = TwelveDataForexProvider(settings=None, api_key='')
    with pytest.raises(MarketDataUnavailableError):
        p.initialize()


# ── Registry gating ──
# Exercise the install gate through its injectable `_register` hook so global
# registry state is never mutated (which would destabilise other tests).

def test_forex_registers_only_when_the_key_is_present(monkeypatch):
    from providers.registry import _install_builtin_providers

    monkeypatch.setenv('TWELVEDATA_API_KEY', 'test-key')
    registered = []
    _install_builtin_providers(lambda cls, **kw: registered.append(cls.name) or cls)
    assert 'binance' in registered
    assert 'twelvedata' in registered


def test_forex_absent_without_the_key(monkeypatch):
    from providers.registry import _install_builtin_providers

    monkeypatch.delenv('TWELVEDATA_API_KEY', raising=False)
    registered = []
    _install_builtin_providers(lambda cls, **kw: registered.append(cls.name) or cls)
    assert 'binance' in registered
    assert 'twelvedata' not in registered
