"""Tests for the Dark-Venus settings: symbols, leverage, sizing, TP targets."""

from config.settings import Settings


def test_only_three_supported_symbols(settings: Settings):
    assert settings.supported_symbols == [
        'TRX/USDT:USDT', 'XRP/USDT:USDT', 'XLM/USDT:USDT',
    ]
    assert settings.is_supported_symbol('TRX/USDT:USDT')
    assert not settings.is_supported_symbol('BTC/USDT:USDT')
    assert not settings.is_supported_symbol('DOGE/USDT:USDT')


def test_timeframe_is_15m(settings: Settings):
    assert settings.timeframe == '15m'


def test_default_and_override_leverage(settings: Settings):
    assert settings.default_leverage == 5
    # Master settings always run at default leverage.
    assert settings.leverage == 5
    # Admin override clamps into [3, 8] and never above the hard cap (10).
    assert Settings.create_account_settings(settings, {'leverage_override': 8}).leverage == 8
    assert Settings.create_account_settings(settings, {'leverage_override': 12}).leverage == 8
    assert Settings.create_account_settings(settings, {'leverage_override': 1}).leverage == 3
    assert Settings.create_account_settings(settings, {'leverage_override': None}).leverage == 5


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
    assert t['layer1_margin'] == 2.0
    assert t['layer2_margin'] == 4.0
    assert t['max_basket_exposure'] == 6.0
    assert t['basket_tp_l1'] == 0.50
    assert t['basket_tp_l2'] == 1.50
    assert t['daily_profit_target'] == 3.0
    assert t['daily_loss_limit'] == 3.0


def test_tier2_config(settings: Settings):
    t = settings.get_tier(50.0)
    assert t['layer1_margin'] == 4.0
    assert t['layer2_margin'] == 8.0
    assert t['max_basket_exposure'] == 12.0
    assert t['basket_tp_l1'] == 0.80
    assert t['basket_tp_l2'] == 2.00
    assert t['daily_profit_target'] == 4.0
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


def test_position_limits(settings: Settings):
    assert settings.max_baskets_per_account == 2
    assert settings.max_basket_per_symbol == 1
    assert settings.max_total_open_positions == 4


def test_validate_clean(settings: Settings):
    assert settings.validate() == []
