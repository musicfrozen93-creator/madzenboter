"""V4 — Stop placement and take-profit level tests (spec §C2)."""

from v4.params import V4Params
from v4.trade_math import compute_stop, take_profit_levels


def test_compute_stop_atr_fallback_long():
    p = V4Params()
    res = compute_stop(entry=100.0, side='long', atr=1.0, params=p)
    assert res.valid
    assert res.stop_price < 100.0
    assert res.stop_pct >= p.stop_floor_pct


def test_compute_stop_floor_applied():
    p = V4Params()
    # Tiny ATR would give a <1% stop; the floor clamps distance up to 1%.
    res = compute_stop(entry=100.0, side='long', atr=0.001, params=p)
    assert res.valid
    assert abs(res.stop_pct - p.stop_floor_pct) < 1e-9
    assert abs(res.stop_price - 99.0) < 1e-6


def test_compute_stop_ceiling_rejects():
    p = V4Params()
    # Large ATR → stop wider than the 6% ceiling → invalid (reject the setup).
    res = compute_stop(entry=100.0, side='long', atr=10.0, params=p)
    assert not res.valid
    assert 'stop_too_wide' in res.reason


def test_compute_stop_structural_level_short():
    res = compute_stop(entry=100.0, side='short', atr=1.0, structural_level=101.0)
    assert res.valid
    assert res.stop_price > 100.0  # stop above entry for a short


def test_compute_stop_rejects_invalid_inputs():
    assert not compute_stop(entry=0.0, side='long', atr=1.0).valid
    assert not compute_stop(entry=100.0, side='sideways', atr=1.0).valid


def test_take_profit_levels_rr_long():
    p = V4Params()
    lv = take_profit_levels(entry=100.0, stop_price=98.0, side='long', params=p)
    assert lv['r_value'] == 2.0
    assert lv['min_rr_price'] == 100.0 + p.min_rr * 2.0                # 103
    assert lv['partial_price'] == 100.0 + p.partial_trigger_rr * 2.0   # 103
    assert lv['target_price'] == 100.0 + p.target_rr * 2.0             # 104


def test_take_profit_levels_rr_short():
    p = V4Params()
    lv = take_profit_levels(entry=100.0, stop_price=102.0, side='short', params=p)
    assert lv['r_value'] == 2.0
    # Targets sit BELOW entry for a short.
    assert lv['partial_price'] == 100.0 - p.partial_trigger_rr * 2.0   # 97
    assert lv['target_price'] == 100.0 - p.target_rr * 2.0             # 96
