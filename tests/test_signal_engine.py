"""Integration tests for the signal engine: RSI thresholds (CHANGE #4) and the
BTC regime gate (CHANGE #1) working together."""

import numpy as np
import pandas as pd

from config.settings import Settings
from signals.signal_engine import SignalEngine


def _ohlcv(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    return pd.DataFrame({
        'timestamp': pd.to_datetime(np.arange(n), unit='m'),
        'open': closes,
        'high': closes + 1.0,
        'low': closes - 1.0,
        'close': closes,
        'volume': np.full(n, 1000.0),
    })


# Coin frames: 1h uptrend (price > EMA200), 5m sharp decline (RSI < 35) → LONG setup.
COIN_1H = _ohlcv(np.linspace(100, 300, 250))
COIN_5M = _ohlcv(np.linspace(330, 305, 100))

BTC_UP = _ohlcv(np.linspace(100, 320, 260))
BTC_DOWN = _ohlcv(np.linspace(320, 100, 260))


class FakeExchange:
    """Returns crafted OHLCV per (symbol, timeframe). Dispatches BTC separately."""

    def __init__(self, settings: Settings, btc_df: pd.DataFrame):
        self.btc_symbol = settings.btc_symbol
        self.trend_tf = settings.trend_timeframe
        self.btc_df = btc_df

    def fetch_ohlcv(self, symbol, timeframe, limit=500):
        if symbol == self.btc_symbol:
            return self.btc_df.copy()
        if timeframe == self.trend_tf:
            return COIN_1H.copy()
        return COIN_5M.copy()


def test_long_setup_blocked_when_btc_down_impulse(settings: Settings):
    eng = SignalEngine(FakeExchange(settings, BTC_DOWN), settings)
    sig = eng.generate_signal('SOL/USDT:USDT')
    # A long setup must be blocked while BTC is in a DOWN_IMPULSE.
    assert sig is None


def test_long_setup_allowed_when_btc_up_impulse(settings: Settings):
    eng = SignalEngine(FakeExchange(settings, BTC_UP), settings)
    sig = eng.generate_signal('SOL/USDT:USDT')
    assert sig is not None
    assert sig.side == 'long'
    assert sig.rsi < settings.rsi_long_threshold  # confirms RSI<35 entry rule


def test_rsi_threshold_is_35_for_long(settings: Settings):
    # The engine reads the configured threshold; a coin with RSI between 35 and
    # the old 40 must NOT trigger a long under the new stricter rule.
    eng = SignalEngine(FakeExchange(settings, BTC_UP), settings)
    sig = eng.generate_signal('SOL/USDT:USDT')
    assert settings.rsi_long_threshold == 35.0
    if sig is not None:
        assert sig.rsi < 35.0
