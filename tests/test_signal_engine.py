"""Integration tests for the mean-reversion signal engine.

A fake exchange returns crafted 15m OHLCV so the entry logic, BTC trend filter,
and pre-trade risk filters can be exercised without any network.
"""

import numpy as np
import pandas as pd

from config.settings import Settings
from signals.indicators import compute_bollinger_bands, compute_rsi
from signals.signal_engine import SignalEngine

SYMBOL = 'TRX/USDT:USDT'


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
    closes = list(np.linspace(100, 300, 400))
    return _df_from_closes(closes)


def _btc_downtrend() -> pd.DataFrame:
    closes = list(np.linspace(300, 100, 400))
    return _df_from_closes(closes)


class FakeExchange:
    """Returns a per-symbol OHLCV map and a fixed ticker spread."""

    def __init__(self, symbol_df: pd.DataFrame, btc_df: pd.DataFrame, spread: float = 0.0):
        self._symbol_df = symbol_df
        self._btc_df = btc_df
        self._spread = spread

    def fetch_ohlcv(self, symbol, timeframe, limit=300):
        if symbol == 'BTC/USDT:USDT':
            return self._btc_df
        return self._symbol_df

    def fetch_ticker(self, symbol):
        last = float(self._symbol_df['close'].iloc[-1])
        return {'last': last, 'spread': self._spread}


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


def test_long_signal_with_bullish_btc(settings: Settings):
    eng = SignalEngine(FakeExchange(_oversold_long_df(), _btc_uptrend()), settings)
    sig = eng.generate_signal(SYMBOL)
    assert sig is not None
    assert sig.side == 'long'
    assert sig.reason


def test_long_blocked_by_bearish_btc(settings: Settings):
    # BTC bearish blocks LONG entries.
    eng = SignalEngine(FakeExchange(_oversold_long_df(), _btc_downtrend()), settings)
    assert eng.generate_signal(SYMBOL) is None


def test_short_signal_with_bearish_btc(settings: Settings):
    eng = SignalEngine(FakeExchange(_overbought_short_df(), _btc_downtrend()), settings)
    sig = eng.generate_signal(SYMBOL)
    assert sig is not None
    assert sig.side == 'short'


def test_short_blocked_by_bullish_btc(settings: Settings):
    eng = SignalEngine(FakeExchange(_overbought_short_df(), _btc_uptrend()), settings)
    assert eng.generate_signal(SYMBOL) is None


def test_unsupported_symbol_rejected(settings: Settings):
    eng = SignalEngine(FakeExchange(_oversold_long_df(), _btc_uptrend()), settings)
    assert eng.generate_signal('DOGE/USDT:USDT') is None


def test_spread_too_high_skips(settings: Settings):
    # Spread above max_spread_pct → skip even a valid setup.
    big_spread = 95.0 * (settings.max_spread_pct * 5)
    eng = SignalEngine(FakeExchange(_oversold_long_df(), _btc_uptrend(), spread=big_spread), settings)
    assert eng.generate_signal(SYMBOL) is None


def test_volume_spike_skips(settings: Settings):
    vols = [1000.0] * 200
    vols[-1] = 1000.0 * (settings.volume_spike_multiplier + 1)  # spike on last bar
    eng = SignalEngine(FakeExchange(_oversold_long_df(volumes=vols), _btc_uptrend()), settings)
    assert eng.generate_signal(SYMBOL) is None
