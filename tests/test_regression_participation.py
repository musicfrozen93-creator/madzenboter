"""ZenGrid V2 — trade-participation regression tests.

Reproduces the post-C/H-fix participation collapse and proves the repairs:

  R1  Wind-down baskets hovering below the fee floor no longer stall the
      full wind_down_max_hours — after half the window they exit at the
      pre-update gross break-even threshold, releasing the position slot.
  R2  The counter-factor notional cap no longer counts terminating
      (wind-down) baskets, so a factor flip no longer hard-blocks every
      new entry on the flipped side.
  R3  The component-cache fingerprint ignores sync-service churn
      (updated_at) while still invalidating on credential/settings changes
      (asserted in test_v2_audit_fixes.test_c4_component_cache).

Run directly:  python tests/test_regression_participation.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import Settings
from core.dto import Basket, RecoveryLayer, Signal
from grid.templates import TradeTemplate
from market.market_state import BtcFactorState, MarketState
from portfolio.manager import PortfolioManager

from tests.test_v2_audit_fixes import build_manager, run_loop


def wind_down_basket(db, *, age_fraction, atr=0.01, side='long'):
    """Active wind-down basket whose wind-down began age_fraction of the
    window ago (e.g. 0.6 = 60% through wind_down_max_hours)."""
    b = Basket(symbol='DOGE/USDT', side=side, atr_at_entry=atr,
               volatility='medium', leverage=8, template='core',
               risk_budget=12.0)
    b.wind_down = True
    b.wind_down_at = time.time() - age_fraction * 12 * 3600.0
    b.add_layer(RecoveryLayer(1, 0.10, 10.0, 800.0, side))
    db.save_basket(b)
    return b.id


# ─────────────────────────────────────────────
# R1 — wind-down slot stall
# ─────────────────────────────────────────────

def test_r1_wind_down_releases_slot_after_half_window(settings):
    # roi 0.3% gross: below the 1.04% fee floor at 8x — the post-H2 code
    # held this basket for the FULL 12h window (the regression).
    price_for_roi_03 = 0.10 + 0.003 * 10.0 / 800.0  # 0.1000375

    # Young wind-down (10% through window): fee floor applies -> stays open
    # (preferred net-positive exit still being waited for).
    pm, ex, db = build_manager(settings)
    young = wind_down_basket(db, age_fraction=0.1)
    run_loop(pm, ex, db, price_for_roi_03)
    assert db.rows[young]['status'] == 'active', \
        'young wind-down should still wait for a net-positive exit'

    # Past half the window: falls back to the pre-update gross threshold ->
    # exits at the first break-even tick and FREES THE SLOT.
    pm2, ex2, db2 = build_manager(settings)
    stale = wind_down_basket(db2, age_fraction=0.6)
    run_loop(pm2, ex2, db2, price_for_roi_03)
    assert db2.rows[stale]['status'] == 'closed', \
        'stale wind-down must exit at gross break-even (regression fix)'
    assert db2.trades[-1].exit_reason == 'wind_down'
    print('R1 wind-down releases slot after half-window fallback: PASS')


def test_r1_timeout_still_caps_the_window(settings):
    # Negative ROI past the full window -> timeout close still works.
    pm, ex, db = build_manager(settings)
    bid = wind_down_basket(db, age_fraction=1.1)
    run_loop(pm, ex, db, 0.0995)  # roi -4%
    assert db.rows[bid]['status'] == 'closed'
    assert db.trades[-1].exit_reason == 'wind_down_timeout'
    print('R1 timeout backstop unchanged: PASS')


# ─────────────────────────────────────────────
# R2 — counter-factor cap vs terminating baskets
# ─────────────────────────────────────────────

def test_r2_counter_factor_ignores_wind_down_notional(settings):
    pm = PortfolioManager(settings)
    up = MarketState(btc_state=BtcFactorState.UP_IMPULSE.value)

    def short_basket(notional_margin, wind_down):
        b = Basket(symbol='XRP/USDT:USDT', side='short', atr_at_entry=0.001,
                   volatility='medium', leverage=8, template='core',
                   risk_budget=1.0)
        b.wind_down = wind_down
        # margin × leverage 8 = notional; choose margin to consume the cap
        b.add_layer(RecoveryLayer(1, 0.5, notional_margin, 1.0, 'short'))
        return b

    sig = Signal(symbol='ADA/USDT:USDT', side='short', strength=.5, atr=.001,
                 market_regime='trending', volatility='medium',
                 current_price=0.5, ema200=0.5, rsi=65)

    balance = 100.0  # counter cap = 50 notional

    # Regression case: a NON-wind-down short holding 48 notional blocks the
    # new scout (48 + 4 > 50) — the cap working as intended on live exposure.
    live = short_basket(6.0, wind_down=False)   # 48 notional
    t, reason = pm.evaluate(sig, TradeTemplate.SCOUT, 0.5, 8, 0.5,
                            [live], balance, up, 0.0)
    assert t is None and 'counter-factor' in reason, (t, reason)

    # Fixed case: the SAME notional in a wind-down (terminating) basket no
    # longer blocks the new entry — the post-flip starvation is gone.
    terminating = short_basket(6.0, wind_down=True)
    t, reason = pm.evaluate(sig, TradeTemplate.SCOUT, 0.5, 8, 0.5,
                            [terminating], balance, up, 0.0)
    assert t == TradeTemplate.SCOUT, (t, reason)
    print('R2 counter-factor cap ignores terminating wind-downs: PASS')


def test_r2_total_notional_cap_still_counts_wind_downs(settings):
    # Wind-down margin is still REAL margin: the total notional cap must
    # keep counting it (only the counter-factor growth cap excludes it).
    pm = PortfolioManager(settings)
    b = Basket(symbol='XRP/USDT:USDT', side='short', atr_at_entry=0.001,
               volatility='medium', leverage=8, template='core',
               risk_budget=1.0)
    b.wind_down = True
    b.add_layer(RecoveryLayer(1, 0.5, 31.0, 1.0, 'short'))  # 248 notional

    sig = Signal(symbol='ADA/USDT:USDT', side='short', strength=.5, atr=.001,
                 market_regime='trending', volatility='medium',
                 current_price=0.5, ema200=0.5, rsi=65)
    # balance 100 -> total cap 250; 248 + 8 > 250 -> SCOUT input is blocked
    t, reason = pm.evaluate(sig, TradeTemplate.SCOUT, 1.0, 8, 0.5,
                            [b], 100.0, None, 0.0)
    assert t is None and 'notional' in reason, (t, reason)
    print('R2 total notional cap still counts wind-down margin: PASS')


if __name__ == '__main__':
    cfg = Settings.load(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'config', 'config.json',
    ))
    test_r1_wind_down_releases_slot_after_half_window(cfg)
    test_r1_timeout_still_caps_the_window(cfg)
    test_r2_counter_factor_ignores_wind_down_notional(cfg)
    test_r2_total_notional_cap_still_counts_wind_downs(cfg)
    print()
    print('ALL PARTICIPATION-REGRESSION TESTS PASSED (R1, R2)')
