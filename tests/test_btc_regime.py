"""Regression tests for the BTC regime filter (CHANGE #1)."""

import numpy as np
import pandas as pd

from config.settings import BtcRegime, Settings
from signals.btc_regime import classify_btc_regime, regime_allows_side


def _df(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        'high': closes + 0.5,
        'low': closes - 0.5,
        'close': closes,
    })


def test_up_impulse(settings: Settings):
    df = _df(np.linspace(100, 320, 260))  # steady strong uptrend
    assert classify_btc_regime(df, settings) == BtcRegime.UP_IMPULSE


def test_down_impulse(settings: Settings):
    df = _df(np.linspace(320, 100, 260))  # steady strong downtrend
    assert classify_btc_regime(df, settings) == BtcRegime.DOWN_IMPULSE


def test_sideways(settings: Settings):
    x = np.arange(260)
    closes = 100 + 2.0 * np.sin(x)  # fast chop, no persistent trend → weak ADX
    assert classify_btc_regime(_df(closes), settings) == BtcRegime.SIDEWAYS


def test_unknown_on_insufficient_data(settings: Settings):
    df = _df(np.linspace(100, 110, 50))  # < ema_period rows
    assert classify_btc_regime(df, settings) == BtcRegime.UNKNOWN


def test_unknown_on_empty(settings: Settings):
    assert classify_btc_regime(pd.DataFrame(), settings) == BtcRegime.UNKNOWN


def test_regime_allows_side_mapping():
    # UP_IMPULSE → long only
    assert regime_allows_side(BtcRegime.UP_IMPULSE, 'long') is True
    assert regime_allows_side(BtcRegime.UP_IMPULSE, 'short') is False
    # DOWN_IMPULSE → short only
    assert regime_allows_side(BtcRegime.DOWN_IMPULSE, 'short') is True
    assert regime_allows_side(BtcRegime.DOWN_IMPULSE, 'long') is False
    # SIDEWAYS → both
    assert regime_allows_side(BtcRegime.SIDEWAYS, 'long') is True
    assert regime_allows_side(BtcRegime.SIDEWAYS, 'short') is True
    # UNKNOWN (fail-safe) → both
    assert regime_allows_side(BtcRegime.UNKNOWN, 'long') is True
    assert regime_allows_side(BtcRegime.UNKNOWN, 'short') is True
