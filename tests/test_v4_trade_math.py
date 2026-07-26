"""V4 Phase 1 — Stop / TP / Partial / Break-even / Trailing tests (spec §C2, §A3)."""

from v4.params import V4Params
from v4.trade_math import TradeState, compute_stop, manage, take_profit_levels


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


def test_compute_stop_ceiling_skips():
    p = V4Params()
    # Large ATR → stop wider than 6% ceiling → invalid (skip).
    res = compute_stop(entry=100.0, side='long', atr=10.0, params=p)
    assert not res.valid
    assert 'stop_too_wide' in res.reason


def test_compute_stop_structural_level_short():
    res = compute_stop(entry=100.0, side='short', atr=1.0, structural_level=101.0)
    assert res.valid
    assert res.stop_price > 100.0  # stop above entry for a short


def test_take_profit_levels_rr():
    p = V4Params()
    lv = take_profit_levels(entry=100.0, stop_price=98.0, side='long', params=p)
    assert lv['r_value'] == 2.0
    assert lv['partial_price'] == 100.0 + p.partial_trigger_rr * 2.0   # 103
    assert lv['target_price'] == 100.0 + p.target_rr * 2.0             # 104


def test_partial_then_breakeven_then_trail_long():
    p = V4Params()
    st = TradeState.open('long', entry=100.0, stop_price=98.0, quantity=10.0, params=p)
    # Partial at 1.5R = 103. Price hits 103 → take 50% + move stop to BE (100).
    acts = manage(st, price=103.0, atr=1.0, params=p)
    assert any(a.type == 'take_partial' for a in acts)
    assert any(a.type == 'move_stop' and a.reason == 'breakeven' for a in acts)
    assert st.partial_done and st.breakeven_set
    assert st.current_stop == 100.0
    assert abs(st.remaining_qty - 5.0) < 1e-9
    # Further up → trail tightens the stop upward (tighten-only).
    acts2 = manage(st, price=110.0, atr=1.0, params=p)
    assert any(a.type == 'move_stop' and a.reason == 'trail' for a in acts2)
    assert st.current_stop > 100.0


def test_trailing_is_tighten_only():
    p = V4Params()
    st = TradeState.open('long', entry=100.0, stop_price=98.0, quantity=10.0, params=p)
    manage(st, price=103.0, atr=1.0, params=p)   # partial + BE at 100
    manage(st, price=110.0, atr=1.0, params=p)   # trail up to 110 - 2*1 = 108
    high_stop = st.current_stop
    # Price pulls back — stop must NOT loosen.
    manage(st, price=105.0, atr=1.0, params=p)
    assert st.current_stop == high_stop


def test_stop_hit_closes_remainder():
    p = V4Params()
    st = TradeState.open('long', entry=100.0, stop_price=98.0, quantity=10.0, params=p)
    acts = manage(st, price=97.5, atr=1.0, params=p)
    assert any(a.type == 'close' and a.reason == 'stop_hit' for a in acts)
    assert st.closed and st.remaining_qty == 0.0


def test_short_side_partial_and_trail():
    p = V4Params()
    st = TradeState.open('short', entry=100.0, stop_price=102.0, quantity=10.0, params=p)
    # Partial at 100 - 1.5*2 = 97.
    acts = manage(st, price=97.0, atr=1.0, params=p)
    assert any(a.type == 'take_partial' for a in acts)
    assert st.current_stop == 100.0
    # Lower → trail tightens downward.
    manage(st, price=90.0, atr=1.0, params=p)
    assert st.current_stop < 100.0
