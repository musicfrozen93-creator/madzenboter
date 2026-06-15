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


def test_basket_margin_cap_is_balance_tiered(settings: Settings):
    # CHANGE #2 — basket margin cap is per balance tier (A/B/C), not a flat %.
    assert settings.get_margin_hard_cap(10) == 2.50    # Tier A
    assert settings.get_margin_hard_cap(50) == 2.50    # Tier A (boundary)
    assert settings.get_margin_hard_cap(75) == 3.50    # Tier B
    assert settings.get_margin_hard_cap(200) == 3.50   # Tier B (boundary)
    assert settings.get_margin_hard_cap(500) == 4.50   # Tier C
    assert settings.get_margin_hard_cap(100_000) == 4.50
    # Global ceiling equals the largest tier cap.
    assert settings.max_basket_margin_usd == 4.5


def test_layer_margins_sum_to_tier_cap(settings: Settings):
    # CHANGE #2 — each tier's two layers sum to exactly that tier's cap.
    tiers = {10: (1.5, 1.0, 2.5), 100: (2.5, 1.0, 3.5), 500: (3.5, 1.0, 4.5)}
    for balance, (l1, l2, cap) in tiers.items():
        assert settings.get_layer_margin(1, balance) == l1
        assert settings.get_layer_margin(2, balance) == l2
        # Layers beyond the pair clamp to the last (the recovery layer).
        assert settings.get_layer_margin(9, balance) == l2
        assert abs((l1 + l2) - cap) < 1e-9
    # Legacy distribution (no balance) clamps and fits the global ceiling.
    assert settings.get_layer_margin(1) == 2.0
    assert settings.get_layer_margin(9) == 1.0
    assert sum(settings.basket_layer_margins_usd) <= settings.max_basket_margin_usd


def test_recovery_max_two_layers(settings: Settings):
    # CHANGE #1 — maximum layers per basket = 2
    assert settings.recovery_max_layers == 2
    assert len(settings.recovery_atr_distances) == 2
    assert len(settings.recovery_margin_multipliers) == 2


def test_basket_sizing_tiers(settings: Settings):
    # CHANGE #2 — fixed per-tier sizing (A/B/C), not a % of balance
    a = settings.get_basket_sizing_tier(25)
    assert (a['layer1'], a['layer2'], a['max_basket']) == (1.5, 1.0, 2.5)
    b = settings.get_basket_sizing_tier(150)
    assert (b['layer1'], b['layer2'], b['max_basket']) == (2.5, 1.0, 3.5)
    c = settings.get_basket_sizing_tier(1000)
    assert (c['layer1'], c['layer2'], c['max_basket']) == (3.5, 1.0, 4.5)
    # Boundaries: $50 → A, $200 → B.
    assert settings.get_basket_sizing_tier(50)['max_basket'] == 2.5
    assert settings.get_basket_sizing_tier(200)['max_basket'] == 3.5


def test_daily_profit_lock_config(settings: Settings):
    # CHANGE #3 — 8→5, 10→8, 12→10, hard stop 15
    assert settings.daily_profit_hard_stop_pct == 0.15
    tiers = {t['gain']: t['floor'] for t in settings.daily_profit_lock_tiers}
    assert tiers == {0.08: 0.05, 0.10: 0.08, 0.12: 0.10}


def test_loss_streak_config(settings: Settings):
    # CHANGE #4 — 3 consecutive losing baskets → 1 hour pause
    assert settings.loss_streak_threshold == 3
    assert settings.loss_streak_pause_seconds == 3600


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
    # CHANGE #2 — tiered basket cap is preserved for account-derived settings
    acct = Settings.create_account_settings(settings, {'risk_pct': 0.05})
    assert acct.get_margin_hard_cap(20) == 2.50    # Tier A
    assert acct.get_margin_hard_cap(500) == 4.50   # Tier C


def test_basket_sl_override_is_ignored(settings: Settings):
    # Hardening — account sl_settings.basket_sl_pct MUST NOT bypass global 15%
    acct = Settings.create_account_settings(
        settings, {'sl_settings': {'basket_sl_pct': 0.40}}
    )
    assert acct.basket_sl_pct == 0.15


def test_other_sl_settings_still_apply(settings: Settings):
    # Non-basket SL knobs remain overridable per account.
    acct = Settings.create_account_settings(
        settings,
        {'sl_settings': {
            'basket_sl_pct': 0.40,             # ignored
            'individual_sl_atr_mult': 5.0,     # applied
            'emergency_sl_account_pct': 0.10,  # applied
        }},
    )
    assert acct.basket_sl_pct == 0.15
    assert acct.individual_sl_atr_mult == 5.0
    assert acct.emergency_sl_account_pct == 0.10


def test_basket_tp_and_profit_protection_not_overridable(settings: Settings):
    # tp_settings can never change the fixed basket TP / profit-protection levels.
    acct = Settings.create_account_settings(
        settings,
        {'tp_settings': {'basket_tp_roi': {'low': 0.50, 'medium': 0.50, 'high': 0.50}}},
    )
    assert acct.basket_tp_target_roi == 0.15
    assert acct.profit_protection_arm_roi == 0.10
    assert acct.profit_protection_floor_roi == 0.08
