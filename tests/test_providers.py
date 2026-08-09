"""Tests for the Market Data Provider abstraction and registry.

The point of these tests is the ABSTRACTION: a provider the analysis layer has
never heard of must slot in without any analysis code changing.
"""

import pandas as pd
import pytest

from providers.base import (
    CANDLE_COLUMNS,
    TIMEFRAMES,
    MarketDataProvider,
    MarketDataUnavailableError,
    UnknownSymbolError,
    UnsupportedTimeframeError,
    normalize_symbol,
    normalize_timeframe,
    sort_timeframes,
    validate_candles,
)
from providers.binance import BinanceProvider
from providers.registry import (
    UnknownProviderError,
    available_markets,
    default_provider_for,
    describe_providers,
    get_provider,
    register,
    reset_for_tests,
    resolve_name,
)
from tests.fakes import StubProvider, make_candles


# ─────────────────────────────────────────────
# Symbol / timeframe normalization
# ─────────────────────────────────────────────

def test_symbol_normalization_accepts_common_forms():
    assert normalize_symbol('btcusdt') == 'BTCUSDT'
    assert normalize_symbol(' BTCUSDT ') == 'BTCUSDT'
    assert normalize_symbol('BTC/USDT') == 'BTCUSDT'
    assert normalize_symbol('eur-usd') == 'EUR_USD'


@pytest.mark.parametrize('bad', ['', '   ', 'B', '!!!', 'BTC USDT', 'DROP TABLE'])
def test_malformed_symbols_are_rejected_at_the_edge(bad):
    with pytest.raises(UnknownSymbolError):
        normalize_symbol(bad)


def test_timeframe_normalization():
    assert normalize_timeframe('15M') == '15m'
    assert normalize_timeframe(' 1h ') == '1h'


@pytest.mark.parametrize('bad', ['', '7m', '2d', 'daily', None])
def test_unknown_timeframes_are_rejected(bad):
    with pytest.raises(UnsupportedTimeframeError):
        normalize_timeframe(bad)


def test_timeframes_sort_by_duration_not_alphabetically():
    assert sort_timeframes(['1d', '5m', '1h', '15m']) == ('5m', '15m', '1h', '1d')


# ─────────────────────────────────────────────
# The candle contract
# ─────────────────────────────────────────────

def test_valid_candles_pass_the_contract():
    df = make_candles(n=50)
    cleaned = validate_candles(df, 'BTCUSDT')
    # Clean data survives intact (same count, same columns).
    assert len(cleaned) == len(df)
    for column in CANDLE_COLUMNS:
        assert column in cleaned.columns


def test_empty_candles_are_rejected():
    with pytest.raises(MarketDataUnavailableError):
        validate_candles(pd.DataFrame(), 'BTCUSDT')


def test_candles_missing_a_column_are_rejected():
    df = make_candles(n=50).drop(columns=['volume'])
    with pytest.raises(MarketDataUnavailableError, match='volume'):
        validate_candles(df, 'BTCUSDT')


def test_too_few_candles_are_rejected():
    with pytest.raises(MarketDataUnavailableError, match='at least'):
        validate_candles(make_candles(n=5), 'BTCUSDT', min_bars=100)


def test_duplicate_timestamps_are_collapsed():
    df = make_candles(n=50)
    doubled = pd.concat([df, df.iloc[[-1]]], ignore_index=True)  # repeat last bar
    cleaned = validate_candles(doubled, 'BTCUSDT')
    assert len(cleaned) == 50
    assert cleaned['timestamp'].is_unique


def test_candles_are_sorted_chronologically():
    df = make_candles(n=50).iloc[::-1].reset_index(drop=True)  # reversed
    cleaned = validate_candles(df, 'BTCUSDT')
    ts = cleaned['timestamp'].tolist()
    assert ts == sorted(ts)


def test_nan_price_rows_are_dropped():
    df = make_candles(n=50)
    df.loc[10, 'close'] = float('nan')
    cleaned = validate_candles(df, 'BTCUSDT')
    assert len(cleaned) == 49
    assert cleaned['close'].notna().all()


def test_impossible_candles_are_dropped():
    df = make_candles(n=50)
    df.loc[5, 'high'] = df.loc[5, 'low'] - 1  # high below low → impossible
    cleaned = validate_candles(df, 'BTCUSDT')
    assert len(cleaned) == 49


def test_non_positive_prices_are_dropped():
    df = make_candles(n=50)
    df.loc[7, 'close'] = 0
    df.loc[8, 'low'] = -5
    cleaned = validate_candles(df, 'BTCUSDT')
    assert len(cleaned) == 48


def test_mostly_corrupt_data_is_rejected_not_analyzed():
    df = make_candles(n=10)
    for i in range(8):
        df.loc[i, 'close'] = float('nan')   # 8 of 10 rows corrupt
    with pytest.raises(MarketDataUnavailableError):
        validate_candles(df, 'BTCUSDT', min_bars=5)


# ─────────────────────────────────────────────
# Provider contract
# ─────────────────────────────────────────────

def test_stub_provider_satisfies_the_interface():
    provider = StubProvider()
    provider.initialize()
    assert isinstance(provider, MarketDataProvider)
    assert provider.initialized
    assert 'BTCUSDT' in provider.list_symbols()
    assert provider.get_symbol_info('BTCUSDT').quote == 'USDT'


def test_provider_rejects_a_timeframe_it_does_not_serve():
    provider = StubProvider()
    assert provider.ensure_timeframe('15m') == '15m'
    # '3d' is a canonical timeframe, but this provider does not offer it.
    with pytest.raises(UnsupportedTimeframeError, match='does not offer'):
        provider.ensure_timeframe('3d')


def test_provider_rejects_an_unlisted_symbol():
    provider = StubProvider()
    with pytest.raises(UnknownSymbolError):
        provider.fetch_candles('DOGEUSDT', '15m')


def test_funding_rate_defaults_to_none_for_venues_without_it():
    # A spot or FX provider inherits the default and returns None, so the
    # analysis layer never has to special-case a market class.
    assert StubProvider().fetch_funding_rate('BTCUSDT') is None


def test_describe_exposes_capabilities_without_network():
    described = StubProvider().describe()
    assert described['provider'] == 'stub'
    assert described['market'] == 'test'
    assert '15m' in described['timeframes']


# ─────────────────────────────────────────────
# Binance provider — symbol translation only (no network)
# ─────────────────────────────────────────────

def test_binance_translates_platform_symbols_to_ccxt_form(settings):
    provider = BinanceProvider(settings)
    assert provider._to_platform('BTC/USDT:USDT') == 'BTCUSDT'
    # With no markets loaded it still derives the venue form for USDT pairs.
    assert provider._to_venue('BTCUSDT') == 'BTC/USDT:USDT'


def test_binance_advertises_timeframes_without_being_initialized(settings):
    # Capability discovery must never require a network round-trip.
    assert '15m' in BinanceProvider.timeframes
    assert set(BinanceProvider.timeframes).issubset(set(TIMEFRAMES))


def test_binance_declares_btc_as_its_benchmark(settings):
    assert BinanceProvider(settings).reference_symbol() == 'BTCUSDT'


# ─────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────

def test_binance_is_registered_as_the_crypto_default():
    assert default_provider_for('crypto') == 'binance'
    assert 'binance' in available_markets()['crypto']


def test_resolve_name_defaults_to_the_market_provider():
    assert resolve_name('crypto') == 'binance'
    assert resolve_name('crypto', 'binance') == 'binance'


def test_resolve_name_rejects_unknown_market_and_provider():
    with pytest.raises(UnknownProviderError):
        resolve_name('commodities')
    with pytest.raises(UnknownProviderError):
        resolve_name('crypto', 'kraken')


def test_resolve_name_rejects_a_provider_from_another_market():
    with pytest.raises(UnknownProviderError, match='serves the'):
        resolve_name('forex', 'binance')


def test_a_new_market_needs_no_analysis_changes(settings):
    """Register a provider for a brand-new market and resolve it end to end."""

    class OandaLikeProvider(StubProvider):
        name = 'oanda_test'
        market = 'forex_test'

    try:
        register(OandaLikeProvider)
        assert default_provider_for('forex_test') == 'oanda_test'
        assert 'oanda_test' in available_markets()['forex_test']

        instance = get_provider(settings, 'forex_test')
        assert isinstance(instance, OandaLikeProvider)
        assert instance.initialized                      # registry initialized it
        assert get_provider(settings, 'forex_test') is instance   # cached
    finally:
        # Keep the registry clean for other tests.
        from providers.registry import _DEFAULT_FOR_MARKET, _REGISTRY

        _REGISTRY.pop('oanda_test', None)
        _DEFAULT_FOR_MARKET.pop('forex_test', None)
        reset_for_tests()


def test_registering_without_name_or_market_is_rejected():
    class Nameless(StubProvider):
        name = ''
        market = ''

    with pytest.raises(ValueError, match='must set both'):
        register(Nameless)


def test_describe_providers_reads_class_attributes_only():
    described = {p['provider']: p for p in describe_providers()}
    assert described['binance']['market'] == 'crypto'
    assert described['binance']['is_default'] is True
    assert '15m' in described['binance']['timeframes']
