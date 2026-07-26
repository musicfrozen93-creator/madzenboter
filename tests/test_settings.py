"""Tests for the Dark-Venus settings: symbols, leverage, sizing, TP targets."""

from config.settings import Settings


def test_expanded_watchlist(settings: Settings):
    # Expanded to 20 correlated USDT-M perps.
    assert len(settings.supported_symbols) == 20
    for base in ('TRX', 'XRP', 'XLM', 'ADA', 'ALGO', 'HBAR', 'VET', 'LINK', 'DOT', 'ATOM',
                 'LTC', 'POL', 'ETC', 'BCH', 'NEAR', 'EOS', 'FIL', 'IOTA', 'GRT', 'AVAX'):
        assert settings.is_supported_symbol(f'{base}/USDT:USDT')
    # Anything outside the list is still blocked.
    assert not settings.is_supported_symbol('BTC/USDT:USDT')
    assert not settings.is_supported_symbol('DOGE/USDT:USDT')


def test_timeframe_is_15m(settings: Settings):
    assert settings.timeframe == '15m'


def test_default_and_override_leverage(settings: Settings):
    assert settings.default_leverage == 8
    # Master settings always run at default leverage.
    assert settings.leverage == 8
    # Admin override clamps into [5, 10] and never above the hard cap (10).
    assert Settings.create_account_settings(settings, {'leverage_override': 7}).leverage == 7
    assert Settings.create_account_settings(settings, {'leverage_override': 10}).leverage == 10
    assert Settings.create_account_settings(settings, {'leverage_override': 12}).leverage == 10
    assert Settings.create_account_settings(settings, {'leverage_override': 3}).leverage == 5
    assert Settings.create_account_settings(settings, {'leverage_override': None}).leverage == 8


def test_never_exceeds_hard_max_leverage(settings: Settings):
    assert settings.hard_max_leverage == 10
    assert settings.clamp_leverage(999) == 10


def test_exactly_two_tiers(settings: Settings):
    assert len(settings.account_tiers) == 2
    assert [t['id'] for t in settings.account_tiers] == ['tier1', 'tier2']


def test_tier_boundaries(settings: Settings):
    # Below the minimum → no tier (must not trade).
    assert settings.get_tier(19.99) is None
    # 20–39.99 → Tier 1; 40+ → Tier 2.
    assert settings.get_tier(20.0)['id'] == 'tier1'
    assert settings.get_tier(39.99)['id'] == 'tier1'
    assert settings.get_tier(40.0)['id'] == 'tier2'
    assert settings.get_tier(100.0)['id'] == 'tier2'


def test_tier1_config(settings: Settings):
    t = settings.get_tier(25.0)
    assert t['layer1_margin'] == 1.0
    assert t['layer2_margin'] == 2.0
    assert t['max_basket_exposure'] == 3.0
    assert t['basket_tp_l1'] == 0.30
    assert t['basket_tp_l2'] == 0.80
    assert t['daily_profit_target'] == 2.0
    assert t['daily_loss_limit'] == 3.0


def test_tier2_config(settings: Settings):
    t = settings.get_tier(50.0)
    assert t['layer1_margin'] == 2.0
    assert t['layer2_margin'] == 4.0
    assert t['max_basket_exposure'] == 6.0
    assert t['basket_tp_l1'] == 0.50
    assert t['basket_tp_l2'] == 1.20
    assert t['daily_profit_target'] == 3.5
    assert t['daily_loss_limit'] == 4.0


def test_tier_layers_fit_exposure(settings: Settings):
    # L1 + L2 must equal the tier's max basket exposure (never exceed it).
    for t in settings.account_tiers:
        assert t['layer1_margin'] + t['layer2_margin'] == t['max_basket_exposure']


def test_tier_or_default_below_minimum(settings: Settings):
    # Managing existing baskets below the min uses the most conservative tier.
    assert settings.get_tier_or_default(10.0)['id'] == 'tier1'


def test_max_two_layers(settings: Settings):
    assert settings.recovery_max_layers == 2


def test_position_limits_are_per_tier(settings: Settings):
    assert settings.max_basket_per_symbol == 1
    t1, t2 = settings.get_tier(25.0), settings.get_tier(50.0)
    assert t1['max_active_symbols'] == 4 and t1['max_positions'] == 8
    assert t2['max_active_symbols'] == 6 and t2['max_positions'] == 12


def test_protection_floors(settings: Settings):
    assert settings.get_tier(25.0)['protection_floor'] == 15.0
    assert settings.get_tier(50.0)['protection_floor'] == 30.0


def test_correlation_min_scores(settings: Settings):
    assert settings.correlation_min_score_first == 2
    assert settings.correlation_min_score_additional == 3


def test_validate_clean(settings: Settings):
    assert settings.validate() == []
