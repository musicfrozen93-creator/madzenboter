"""Regression tests for the upgraded Settings / config values."""

from config.settings import Settings


def test_rsi_thresholds(settings: Settings):
    # CHANGE #4 — LONG = RSI < 35, SHORT = RSI > 65
    assert settings.rsi_long_threshold == 35.0
    assert settings.rsi_short_threshold == 65.0


def test_watchlist_size(settings: Settings):
    # CHANGE #3 — watchlist 25 → 50
    assert settings.max_watchlist_size == 50


def test_basket_sl(settings: Settings):
    # CHANGE #5 — basket_sl_pct 20% → 15%
    assert settings.basket_sl_pct == 0.15


def test_profit_protection_levels(settings: Settings):
    # CHANGE #6 — target 15%, arm 10%, floor 8%
    assert settings.basket_tp_target_roi == 0.15
    assert settings.profit_protection_arm_roi == 0.10
    assert settings.profit_protection_floor_roi == 0.08
    assert settings.profit_protection_floor_roi < settings.profit_protection_arm_roi
    assert settings.profit_protection_arm_roi < settings.basket_tp_target_roi


def test_max_positions_fixed_for_all_balances(settings: Settings):
    # CHANGE #7 — every account uses max_positions = 8 regardless of balance
    for balance in (5, 20, 50, 100, 500, 100_000):
        assert settings.get_max_positions(balance) == 8
    assert settings.max_positions == 8


def test_basket_margin_cap_fixed_at_5(settings: Settings):
    # CHANGE #8 — fixed $5 total basket margin cap, independent of balance
    for balance in (5, 20, 50, 100, 500, 100_000):
        assert settings.get_margin_hard_cap(balance) == 5.0
    assert settings.max_basket_margin_usd == 5.0


def test_layer_margins_sum_to_cap(settings: Settings):
    # CHANGE #8 — per-layer distribution sums to exactly $5
    margins = settings.basket_layer_margins_usd
    assert margins == [2.0, 1.0, 1.0, 1.0]
    assert abs(sum(margins) - settings.max_basket_margin_usd) < 1e-9
    # get_layer_margin is 1-based and clamps beyond the list
    assert settings.get_layer_margin(1) == 2.0
    assert settings.get_layer_margin(2) == 1.0
    assert settings.get_layer_margin(4) == 1.0
    assert settings.get_layer_margin(9) == 1.0  # clamped to last


def test_btc_regime_settings(settings: Settings):
    # CHANGE #1 — BTC regime filter enabled
    assert settings.btc_regime_filter_enabled is True
    assert settings.btc_symbol == 'BTC/USDT:USDT'


def test_cooldown_setting(settings: Settings):
    # CHANGE #2 — 30 minute cooldown
    assert settings.symbol_cooldown_seconds == 1800


def test_config_validates_clean(settings: Settings):
    assert settings.validate() == []


def test_account_overrides_do_not_change_max_positions(settings: Settings):
    # CHANGE #7 — per-account max_positions override is ignored (all use 8)
    acct = Settings.create_account_settings(settings, {'max_positions': 2})
    assert acct.get_max_positions(50) == 8


def test_account_overrides_do_not_change_margin_cap(settings: Settings):
    # CHANGE #8 — basket cap stays $5 for account-derived settings
    acct = Settings.create_account_settings(settings, {'risk_pct': 0.05})
    assert acct.get_margin_hard_cap(20) == 5.0
