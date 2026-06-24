"""Tests for the single-entry scalping settings: universe, leverage, tiers, TP/SL."""

from config.settings import Settings


def test_fixed_universe_is_100_symbols(settings: Settings):
    assert len(settings.supported_symbols) == 100
    assert len(set(settings.supported_symbols)) == 100        # no duplicates
    # A sample of the curated universe is present.
    for base in ('SOL', 'XRP', 'DOGE', 'LINK', 'TRX', 'ARB', 'INJ', 'WIF'):
        assert settings.is_supported_symbol(f'{base}/USDT:USDT')
    # BTC and ETH are NEVER traded (BTC is the filter reference only).
    assert not settings.is_supported_symbol('BTC/USDT:USDT')
    assert not settings.is_supported_symbol('ETH/USDT:USDT')


def test_timeframe_is_15m(settings: Settings):
    assert settings.timeframe == '15m'


def test_default_and_override_leverage(settings: Settings):
    assert settings.default_leverage == 10
    assert settings.leverage == 10
    # Admin override clamps into [8, 10] and never above the hard cap (10).
    assert Settings.create_account_settings(settings, {'leverage_override': 9}).leverage == 9
    assert Settings.create_account_settings(settings, {'leverage_override': 10}).leverage == 10
    assert Settings.create_account_settings(settings, {'leverage_override': 12}).leverage == 10
    assert Settings.create_account_settings(settings, {'leverage_override': 5}).leverage == 8
    assert Settings.create_account_settings(settings, {'leverage_override': None}).leverage == 10


def test_never_exceeds_hard_max_leverage(settings: Settings):
    assert settings.hard_max_leverage == 10
    assert settings.clamp_leverage(999) == 10


def test_exactly_two_tiers(settings: Settings):
    assert len(settings.account_tiers) == 2
    assert [t['id'] for t in settings.account_tiers] == ['tier1', 'tier2']


def test_tier_boundaries(settings: Settings):
    assert settings.get_tier(19.99) is None              # below the minimum → no tier
    assert settings.get_tier(20.0)['id'] == 'tier1'
    assert settings.get_tier(39.99)['id'] == 'tier1'
    assert settings.get_tier(40.0)['id'] == 'tier2'
    assert settings.get_tier(100.0)['id'] == 'tier2'


def test_tier1_config(settings: Settings):
    t = settings.get_tier(25.0)
    assert t['margin_per_trade'] == 0.8
    assert t['max_active_symbols'] == 8
    assert t['max_positions'] == 8
    assert t['daily_profit_target'] == 2.0
    assert t['daily_loss_limit'] == 3.0
    assert t['protection_floor'] == 15.0


def test_tier2_config(settings: Settings):
    t = settings.get_tier(50.0)
    assert t['margin_per_trade'] == 1.5
    assert t['max_active_symbols'] == 10
    assert t['max_positions'] == 10
    assert t['daily_profit_target'] == 3.5
    assert t['daily_loss_limit'] == 4.0
    assert t['protection_floor'] == 30.0


def test_tp_sl_percentages(settings: Settings):
    assert settings.tp_margin_pct == 0.20
    assert settings.sl_margin_pct == 0.12
    assert settings.tp_margin_pct > settings.sl_margin_pct


def test_symbol_cooldown_is_30_minutes(settings: Settings):
    assert settings.symbol_cooldown_seconds == 1800


def test_portfolio_lock_thresholds_per_tier(settings: Settings):
    t1, t2 = settings.get_tier(25.0), settings.get_tier(50.0)
    assert t1['portfolio_lock_trigger'] == 0.50 and t1['portfolio_lock_floor'] == 0.35
    assert t2['portfolio_lock_trigger'] == 0.80 and t2['portfolio_lock_floor'] == 0.50
    # Dynamic protection bands [peak_threshold, pct].
    assert t1['portfolio_protection_bands'] == [[0.50, 0.70], [1.00, 0.75], [1.50, 0.80], [2.00, 0.85]]
    assert t2['portfolio_protection_bands'] == [[0.80, 0.70], [2.00, 0.75], [3.00, 0.80], [4.00, 0.85]]
    # Trigger must always exceed the minimum-protected floor.
    for t in settings.account_tiers:
        assert t['portfolio_lock_trigger'] > t['portfolio_lock_floor'] > 0


def test_atr_band_and_signal_score(settings: Settings):
    assert settings.atr_entry_min_pct == 0.003
    assert settings.atr_entry_max_pct == 0.012
    assert settings.atr_entry_min_pct < settings.atr_entry_max_pct
    assert settings.min_signal_score == 1


def test_single_entry_one_position_per_symbol(settings: Settings):
    assert settings.max_basket_per_symbol == 1


def test_position_notional_clears_min_notional(settings: Settings):
    # Tier 1: 0.8 × 10 = $8 ≥ $5 floor; Tier 2: 1.5 × 10 = $15 ≥ $5 floor.
    floor = settings.min_notional_floor
    for t in settings.account_tiers:
        assert t['margin_per_trade'] * settings.default_leverage >= floor


def test_tier_or_default_below_minimum(settings: Settings):
    assert settings.get_tier_or_default(10.0)['id'] == 'tier1'


def test_realistic_taker_fee(settings: Settings):
    assert settings.taker_fee_pct == 0.0005


def test_validate_clean(settings: Settings):
    assert settings.validate() == []
