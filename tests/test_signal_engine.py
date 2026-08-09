"""Integration tests for the mean-reversion setup detector.

A fake MARKET DATA PROVIDER returns crafted OHLCV so the entry logic, benchmark
trend filter, and market-quality filters can be exercised without any network.
Using the provider interface here (not an exchange client) is the point: the
detector must work against any venue.
"""

import numpy as np
import pandas as pd

from config.settings import Settings
from providers.base import MarketDataProvider, Quote, SymbolInfo, UnknownSymbolError
from signals.indicators import compute_bollinger_bands, compute_rsi
from signals.signal_engine import SignalEngine

SYMBOL = 'TRXUSDT'
BENCHMARK = 'BTCUSDT'


def _df_from_closes(closes, last_low=None, last_high=None, volumes=None) -> pd.DataFrame:
    closes = pd.Series(closes, dtype=float)
    n = len(closes)
    # Small bodies (open ≈ close) so the "news candle" body filter never trips.
    opens = closes + 0.001
    highs = closes + 0.01
    lows = closes - 0.01
    if last_low is not None:
        lows.iloc[-1] = last_low
    if last_high is not None:
        highs.iloc[-1] = last_high
    vol = pd.Series(volumes if volumes is not None else [1000.0] * n, dtype=float)
    return pd.DataFrame({
        'timestamp': range(n), 'open': opens, 'high': highs,
        'low': lows, 'close': closes, 'volume': vol,
    })


def _btc_uptrend() -> pd.DataFrame:
    return _df_from_closes(list(np.linspace(100, 300, 400)))


def _btc_downtrend() -> pd.DataFrame:
    return _df_from_closes(list(np.linspace(300, 100, 400)))


class FakeProvider(MarketDataProvider):
    """In-memory provider: a per-symbol OHLCV map and a fixed quote spread."""

    name = 'fake'
    market = 'test'
    timeframes = ('15m', '1h')

    def __init__(self, symbol_df: pd.DataFrame, btc_df: pd.DataFrame, spread: float = 0.0):
        self._symbol_df = symbol_df
        self._btc_df = btc_df
        self._spread = spread

    def initialize(self) -> None:
        pass

    def list_symbols(self):
        return [SYMBOL, BENCHMARK]

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        return SymbolInfo(symbol=symbol, base=symbol[:-4], quote='USDT', price_precision=6)

    def fetch_candles(self, symbol, timeframe, limit=300):
        self.ensure_timeframe(timeframe)
        if symbol == BENCHMARK:
            return self._btc_df
        if symbol != SYMBOL:
            raise UnknownSymbolError(f'{symbol} is not listed')
        return self._symbol_df

    def fetch_quote(self, symbol) -> Quote:
        last = float(self._symbol_df['close'].iloc[-1])
        return Quote(symbol=symbol, last=last, bid=last, ask=last, spread=self._spread)

    def reference_symbol(self):
        return BENCHMARK


def _trend_with_osc(trend_lo: float, trend_hi: float, n: int = 200, amp: float = 0.08):
    """Linear trend plus a small ±amp oscillation.

    The oscillation guarantees both up and down moves each bar so RSI is always
    defined while the trend keeps it at an extreme (≈11 declining, ≈90 rising).
    """
    i = np.arange(n)
    return pd.Series(np.linspace(trend_lo, trend_hi, n) + amp * ((i % 2) * 2 - 1), dtype=float)


def _oversold_long_df(volumes=None) -> pd.DataFrame:
    # Declining trend → RSI ≈ 11; final bar wicks just below the lower BB.
    closes = _trend_with_osc(120.0, 95.0)
    _, _, lower = compute_bollinger_bands(closes, 20, 2.0)
    return _df_from_closes(closes, last_low=float(lower.iloc[-1]) - 0.10, volumes=volumes)


def _overbought_short_df(volumes=None) -> pd.DataFrame:
    # Rising trend → RSI ≈ 90; final bar wicks just above the upper BB.
    closes = _trend_with_osc(80.0, 105.0)
    _, upper, _ = compute_bollinger_bands(closes, 20, 2.0)
    return _df_from_closes(closes, last_high=float(upper.iloc[-1]) + 0.10, volumes=volumes)


def test_oversold_long_setup_is_valid(settings: Settings):
    # Sanity-check the crafted data actually represents a long setup.
    df = _oversold_long_df()
    rsi = compute_rsi(df['close'], settings.rsi_period).dropna().iloc[-1]
    _, _, lower = compute_bollinger_bands(df['close'], settings.bb_period, settings.bb_std)
    assert rsi < settings.rsi_oversold
    assert df['low'].iloc[-1] <= lower.iloc[-1]


def test_long_signal_with_bullish_benchmark(settings: Settings):
    eng = SignalEngine(FakeProvider(_oversold_long_df(), _btc_uptrend()), settings)
    sig = eng.generate_signal(SYMBOL)
    assert sig is not None
    assert sig.side == 'long'
    assert sig.reason


def test_signal_strength_score_is_scored(settings: Settings):
    # Strong oversold long under a bullish benchmark: extreme RSI (<20) + aligned
    # benchmark + good spread/liquidity ⇒ score >= 3 (capped at 4).
    eng = SignalEngine(FakeProvider(_oversold_long_df(), _btc_uptrend()), settings)
    sig = eng.generate_signal(SYMBOL)
    assert sig is not None
    assert 0 <= sig.strength_score <= 4
    assert sig.strength_score >= 3


def test_long_blocked_by_bearish_benchmark(settings: Settings):
    eng = SignalEngine(FakeProvider(_oversold_long_df(), _btc_downtrend()), settings)
    assert eng.generate_signal(SYMBOL) is None


def test_short_signal_with_bearish_benchmark(settings: Settings):
    eng = SignalEngine(FakeProvider(_overbought_short_df(), _btc_downtrend()), settings)
    sig = eng.generate_signal(SYMBOL)
    assert sig is not None
    assert sig.side == 'short'


def test_short_blocked_by_bullish_benchmark(settings: Settings):
    eng = SignalEngine(FakeProvider(_overbought_short_df(), _btc_uptrend()), settings)
    assert eng.generate_signal(SYMBOL) is None


def test_symbol_the_provider_does_not_list_is_skipped(settings: Settings):
    # The PROVIDER is the authority on which symbols exist, not the engine.
    eng = SignalEngine(FakeProvider(_oversold_long_df(), _btc_uptrend()), settings)
    assert eng.generate_signal('DOGEUSDT') is None


def test_timeframe_is_validated_against_the_provider(settings: Settings):
    eng = SignalEngine(FakeProvider(_oversold_long_df(), _btc_uptrend()), settings)
    # '4h' is outside FakeProvider.timeframes — the setup is skipped, not guessed.
    assert eng.generate_signal(SYMBOL, timeframe='4h') is None


def test_spread_too_high_skips(settings: Settings):
    big_spread = 95.0 * (settings.max_spread_pct * 5)
    eng = SignalEngine(
        FakeProvider(_oversold_long_df(), _btc_uptrend(), spread=big_spread), settings
    )
    assert eng.generate_signal(SYMBOL) is None


def test_volume_spike_skips(settings: Settings):
    vols = [1000.0] * 200
    vols[-1] = 1000.0 * (settings.volume_spike_multiplier + 1)  # spike on last bar
    eng = SignalEngine(FakeProvider(_oversold_long_df(volumes=vols), _btc_uptrend()), settings)
    assert eng.generate_signal(SYMBOL) is None
