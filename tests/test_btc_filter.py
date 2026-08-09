"""Tests for the BTC 15m trend filter."""

import numpy as np
import pandas as pd

from config.settings import BtcRegime, Settings
from signals.btc_regime import classify_btc_regime, regime_allows_side


def _df(prices) -> pd.DataFrame:
    closes = pd.Series(prices, dtype=float)
    return pd.DataFrame({
        'open': closes, 'high': closes, 'low': closes,
        'close': closes, 'volume': pd.Series([1.0] * len(closes)),
    })


def test_bullish_when_uptrend(settings: Settings):
    # Strong, smooth uptrend → price > EMA200 and EMA50 > EMA200 → BULLISH.
    prices = list(np.linspace(100, 300, 400))
    assert classify_btc_regime(_df(prices), settings) == BtcRegime.BULLISH


def test_bearish_when_downtrend(settings: Settings):
    prices = list(np.linspace(300, 100, 400))
    assert classify_btc_regime(_df(prices), settings) == BtcRegime.BEARISH


def test_unknown_when_insufficient_data(settings: Settings):
    prices = list(np.linspace(100, 110, 50))  # < EMA200 period
    assert classify_btc_regime(_df(prices), settings) == BtcRegime.UNKNOWN


def test_regime_allows_side():
    # Bullish blocks shorts.
    assert regime_allows_side(BtcRegime.BULLISH, 'long')
    assert not regime_allows_side(BtcRegime.BULLISH, 'short')
    # Bearish blocks longs.
    assert regime_allows_side(BtcRegime.BEARISH, 'short')
    assert not regime_allows_side(BtcRegime.BEARISH, 'long')
    # Neutral / unknown allow both (fail-safe).
    for r in (BtcRegime.NEUTRAL, BtcRegime.UNKNOWN):
        assert regime_allows_side(r, 'long')
        assert regime_allows_side(r, 'short')
