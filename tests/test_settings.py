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


def test_fixed_layer_margins(settings: Settings):
    # Layer 2 is exactly 2x Layer 1 (the single recovery layer).
    l1 = settings.get_layer_margin(1)
    l2 = settings.get_layer_margin(2)
    assert l1 == settings.layer1_margin_usd
    assert l2 == l1 * settings.layer2_margin_multiplier
    # Sizing must NOT depend on balance — there is no balance parameter at all.


def test_basket_tp_targets(settings: Settings):
    # Layer 1 only -> ~$0.50; with the recovery layer -> ~$1.50-$2.00.
    assert settings.basket_tp_target_usd(1) == settings.basket_tp_layer1_usd
    assert settings.basket_tp_target_usd(2) == settings.basket_tp_recovery_usd
    assert 1.50 <= settings.basket_tp_recovery_usd <= 2.00


def test_max_two_layers(settings: Settings):
    assert settings.recovery_max_layers == 2


def test_daily_limits(settings: Settings):
    assert settings.daily_profit_target_usd == 5.0
    assert settings.daily_loss_limit_usd == 3.0


def test_max_two_baskets(settings: Settings):
    assert settings.max_baskets_per_account == 2
    assert settings.max_basket_per_symbol == 1


def test_validate_clean(settings: Settings):
    assert settings.validate() == []
