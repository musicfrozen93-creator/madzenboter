"""Regression tests for the tightened basket stop-loss (CHANGE #5)."""

from config.settings import Settings
from core.dto import Basket, RecoveryLayer
from risk.stop_loss import StopLossManager


def _long_basket() -> Basket:
    b = Basket(symbol='X/USDT:USDT', side='long', atr_at_entry=1.0, volatility='medium')
    b.add_layer(RecoveryLayer(1, entry_price=100.0, margin=1.0, quantity=1.0, side='long'))
    return b


def test_basket_sl_fires_at_15pct_loss(settings: Settings):
    sl = StopLossManager(settings)
    b = _long_basket()
    # loss fraction == (100 - price) with margin=1, qty=1
    assert sl.check_basket_sl(b, 99.85) is True   # 15% loss
    assert sl.check_basket_sl(b, 99.86) is False  # 14% loss
    assert sl.check_basket_sl(b, 100.0) is False  # no loss


def test_basket_sl_threshold_value(settings: Settings):
    assert settings.basket_sl_pct == 0.15
