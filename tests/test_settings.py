"""Tests for the analysis settings: pairs, timeframe, indicators, filters."""

from config.settings import Settings


def test_pair_list(settings: Settings):
    # 20 liquid USDT-M perps are available for analysis.
    assert len(settings.supported_symbols) == 20
    for base in ('TRX', 'XRP', 'XLM', 'ADA', 'ALGO', 'HBAR', 'VET', 'LINK', 'DOT', 'ATOM',
                 'LTC', 'POL', 'ETC', 'BCH', 'NEAR', 'EOS', 'FIL', 'IOTA', 'GRT', 'AVAX'):
        assert settings.is_supported_symbol(f'{base}/USDT:USDT')
    assert not settings.is_supported_symbol('DOGE/USDT:USDT')


def test_default_timeframe_is_15m(settings: Settings):
    assert settings.timeframe == '15m'
    assert settings.candle_limit >= 200


def test_indicator_periods(settings: Settings):
    assert settings.rsi_period == 14
    assert settings.atr_period == 14
    assert settings.bb_period == 20
    assert settings.bb_std == 2.0
    assert settings.rsi_oversold == 30.0
    assert settings.rsi_overbought == 70.0


def test_btc_regime_context(settings: Settings):
    assert settings.btc_symbol == 'BTC/USDT:USDT'
    assert settings.btc_ema_fast == 50
    assert settings.btc_ema_slow == 200
    assert settings.btc_ema_fast < settings.btc_ema_slow


def test_market_quality_filters(settings: Settings):
    assert settings.risk_filter_lookback == 30
    assert settings.max_spread_pct == 0.0010
    assert settings.atr_explosion_multiplier == 2.5
    assert settings.news_candle_atr_multiplier == 2.5
    assert settings.volume_spike_multiplier == 3.0


def test_no_execution_parameters_remain(settings: Settings):
    # Phase 0 guarantee: nothing here can size, leverage, or place a trade.
    for removed in (
        'default_leverage', 'max_leverage', 'hard_max_leverage', 'leverage',
        'account_tiers', 'min_tier_balance', 'recovery_max_layers',
        'basket_hard_sl_usd', 'symbol_cooldown_seconds', 'taker_fee_pct',
        'api_key', 'api_secret', 'master_encryption_key', 'admin_api_key',
    ):
        assert not hasattr(settings, removed), f'{removed} should have been removed'


def test_validate_clean(settings: Settings):
    assert settings.validate() == []
