"""V4 Phase 1 — Directional Heat + Circuit Breaker tests (spec §A2, §A3, §A4, §C2)."""

from v4.params import V4Params
from v4.portfolio import (
    CircuitInputs,
    check_directional_heat,
    evaluate_circuit_breakers,
)


def test_directional_heat_blocks_over_cap_same_side():
    p = V4Params()  # cap 3% of equity
    # equity 1000 → cap $30. Already $28 long, adding $5 long → $33 > $30 → blocked.
    d = check_directional_heat('long', 5.0, 28.0, 0.0, 1000.0, p)
    assert not d.allowed
    assert 'directional_heat_cap' in d.reason


def test_directional_heat_allows_opposite_side():
    p = V4Params()
    # $28 long already, adding a SHORT does not touch the long bucket.
    d = check_directional_heat('short', 5.0, 28.0, 0.0, 1000.0, p)
    assert d.allowed


def test_daily_loss_flattens():
    p = V4Params()
    dec = evaluate_circuit_breakers(CircuitInputs(
        daily_pnl_pct=-0.05, weekly_pnl_pct=-0.05, monthly_pnl_pct=-0.05,
        drawdown_pct=0.05, consecutive_losses=0,
    ), p)
    assert dec.level == 'daily_loss'
    assert dec.must_flatten and not dec.allow_new_entries


def test_daily_profit_locks_without_flatten():
    p = V4Params()
    dec = evaluate_circuit_breakers(CircuitInputs(
        daily_pnl_pct=0.04, weekly_pnl_pct=0.04, monthly_pnl_pct=0.04,
        drawdown_pct=0.0, consecutive_losses=0, day_peak_pnl_pct=0.04,
    ), p)
    assert dec.level == 'daily_profit_lock'
    assert not dec.allow_new_entries
    assert dec.tighten_trails
    assert not dec.must_flatten            # runners protected, not force-closed (§A3)


def test_giveback_protection_without_flatten():
    p = V4Params()
    # Peaked at +3%, now +1% → gave back 2/3 (> 40%) of peak → protect, don't flatten.
    dec = evaluate_circuit_breakers(CircuitInputs(
        daily_pnl_pct=0.01, weekly_pnl_pct=0.01, monthly_pnl_pct=0.01,
        drawdown_pct=0.0, consecutive_losses=0, day_peak_pnl_pct=0.03,
    ), p)
    assert dec.level == 'daily_giveback'
    assert not dec.allow_new_entries and not dec.must_flatten


def test_max_drawdown_halts():
    p = V4Params()
    dec = evaluate_circuit_breakers(CircuitInputs(
        daily_pnl_pct=-0.02, weekly_pnl_pct=-0.05, monthly_pnl_pct=-0.10,
        drawdown_pct=0.20, consecutive_losses=0,
    ), p)
    assert dec.level == 'max_drawdown'
    assert dec.halted and not dec.allow_new_entries


def test_severity_order_drawdown_beats_daily():
    p = V4Params()
    # Both daily-loss and max-drawdown tripped → drawdown (stricter) wins.
    dec = evaluate_circuit_breakers(CircuitInputs(
        daily_pnl_pct=-0.05, weekly_pnl_pct=-0.10, monthly_pnl_pct=-0.15,
        drawdown_pct=0.25, consecutive_losses=5,
    ), p)
    assert dec.level == 'max_drawdown'


def test_consecutive_loss_cooldown_halves_risk():
    p = V4Params()
    dec = evaluate_circuit_breakers(CircuitInputs(
        daily_pnl_pct=-0.01, weekly_pnl_pct=-0.01, monthly_pnl_pct=-0.01,
        drawdown_pct=0.02, consecutive_losses=4,
    ), p)
    assert dec.level == 'loss_cooldown'
    assert dec.allow_new_entries
    assert dec.risk_multiplier == p.cooldown_risk_multiplier


def test_normal_state_allows_trading():
    dec = evaluate_circuit_breakers(CircuitInputs(
        daily_pnl_pct=0.01, weekly_pnl_pct=0.02, monthly_pnl_pct=0.03,
        drawdown_pct=0.01, consecutive_losses=1,
    ))
    assert dec.level == 'normal'
    assert dec.allow_new_entries and dec.risk_multiplier == 1.0
