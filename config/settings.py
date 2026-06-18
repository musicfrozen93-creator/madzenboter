"""
ZenGrid — Central Settings, Constants, and Enums.

Dark-Venus-inspired basket recovery core for Binance USDT-M Futures.

This module is the SINGLE source of truth for all strategy parameters. The
strategy is deliberately minimal and identical for every account:

  • Mean-reversion entries (RSI + Bollinger touch) on the 15m timeframe
  • A global BTC 15m trend filter gates trade direction
  • Controlled 2-layer recovery basket (Layer 1 + ONE recovery layer) with
    ATR-based spacing — never a martingale, never Layer 3+
  • Basket take-profit closes the whole basket at a fixed USDT profit target
  • FIXED position sizing — account balance never changes margin, position
    count, recovery size, or layer count
  • Per-account daily profit target ($5) and daily loss limit ($3)

Multi-account platform fields (DATABASE_URL, MASTER_ENCRYPTION_KEY, admin API)
are preserved unchanged so the existing infrastructure keeps working.
"""

import json
import logging
import os
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────

class Side(str, Enum):
    """Trade direction."""
    LONG = 'long'
    SHORT = 'short'


class BtcRegime(str, Enum):
    """BTC 15m trend regime used as a global trade-direction filter.

    BULLISH → BTC above 200 EMA and EMA50 above EMA200 → block SHORT
    BEARISH → BTC below 200 EMA and EMA50 below EMA200 → block LONG
    NEUTRAL → no clear trend                            → allow BOTH
    UNKNOWN → BTC data unavailable (fail-safe)          → allow BOTH
    """
    BULLISH = 'bullish'
    BEARISH = 'bearish'
    NEUTRAL = 'neutral'
    UNKNOWN = 'unknown'


# ─────────────────────────────────────────────
# Settings Dataclass
# ─────────────────────────────────────────────

@dataclass
class Settings:
    """Central configuration container loaded from config.json."""

    # ── API (per-account credentials come from the DB; this stays keyless) ──
    api_key: str = ''
    api_secret: str = ''
    use_testnet: bool = False

    # ── Database ──
    database_url: str = 'postgresql://zengrid:zengrid@localhost:5432/zengrid'

    # ── Encryption ──
    master_encryption_key: str = ''

    # ── Admin API ──
    admin_api_key: str = ''
    admin_api_port: int = 8000

    # ── Supported symbols (ONLY these — anything else is rejected) ──
    supported_symbols: list = field(
        default_factory=lambda: [
            'TRX/USDT:USDT',
            'XRP/USDT:USDT',
            'XLM/USDT:USDT',
        ]
    )
    btc_symbol: str = 'BTC/USDT:USDT'

    # ── Timeframe (15-minute candles only) ──
    timeframe: str = '15m'
    candle_limit: int = 300

    # ── Loop timing ──
    loop_interval_seconds: int = 10
    # New-entry signal generation is throttled to this; exit management always
    # runs on every loop iteration (never delayed behind signal generation).
    signal_eval_interval_seconds: int = 30

    # ── Indicators ──
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14

    # ── BTC trend filter (15m EMA50 / EMA200) ──
    btc_filter_enabled: bool = True
    btc_ema_fast: int = 50
    btc_ema_slow: int = 200
    # Cache the computed BTC regime for this many seconds to avoid refetching
    # BTC candles for every symbol evaluated within a loop.
    btc_regime_cache_seconds: int = 300

    # ── Leverage (default 5x; admin may override 3x–8x; NEVER exceed 10x) ──
    default_leverage: int = 5
    min_leverage: int = 3
    max_leverage: int = 8
    hard_max_leverage: int = 10
    # Resolved leverage actually used by an account (set per-account by
    # create_account_settings; defaults to default_leverage for the master).
    leverage: int = 5

    # ── Position sizing (FIXED — identical for every account, NEVER balance-scaled) ──
    # Layer 1 deploys a fixed margin in USDT. Layer 2 (the single recovery
    # layer) deploys layer2_margin_multiplier × that margin (2× by default).
    layer1_margin_usd: float = 5.0
    layer2_margin_multiplier: float = 2.0
    # Absolute notional floor — Binance rejects dust orders below ~$5 notional.
    min_notional_floor: float = 5.0

    # ── Recovery model (max 2 layers — NO Layer 3/4/5, never a martingale) ──
    recovery_max_layers: int = 2
    # Layer 2 activates only when Layer 1 drawdown exceeds an ATR-based
    # distance:  Layer2Distance = ATR(14) × layer2_atr_multiplier.
    # Volatility-adjusted spacing — NOT fixed grid spacing.
    layer2_atr_multiplier: float = 2.0

    # ── Basket take-profit (fixed USDT net-profit targets) ──
    # Layer 1 only          → close the basket at ≈ $0.50 net profit.
    # Layer 1 + Layer 2     → close the basket at ≈ $1.50–$2.00 net profit.
    basket_tp_layer1_usd: float = 0.50
    basket_tp_recovery_usd: float = 1.75   # midpoint of the $1.50–$2.00 band

    # ── Per-account limits ──
    max_baskets_per_account: int = 2        # max simultaneous symbols/baskets
    max_basket_per_symbol: int = 1          # never two baskets on one symbol
    daily_profit_target_usd: float = 5.0    # stop NEW entries when reached
    daily_loss_limit_usd: float = 3.0       # close ALL baskets + stop when reached

    # ── Pre-trade risk-rule skip filters (skip the trade if ANY trips) ──
    risk_filter_lookback: int = 30          # bars for ATR/volume averages
    max_spread_pct: float = 0.0010          # spread / price ceiling (0.10%)
    atr_explosion_multiplier: float = 2.5   # current ATR > 2.5× average ATR
    news_candle_atr_multiplier: float = 2.5 # candle body > 2.5× ATR (news candle)
    volume_spike_multiplier: float = 3.0    # last volume > 3× average volume

    # ── Same-symbol cooldown after a basket closes (per-account, persisted) ──
    symbol_cooldown_seconds: int = 900      # 15 min (one 15m candle)

    # ── Fees (used for net-profit estimation on basket TP) ──
    taker_fee_pct: float = 0.0004

    # ── Logging ──
    log_level: str = 'INFO'

    # ─────────────────────────────────────────
    # Class Methods
    # ─────────────────────────────────────────

    @classmethod
    def load(cls, config_path: str = 'config/config.json') -> 'Settings':
        """Load settings from a JSON configuration file.

        Raises:
            FileNotFoundError: If the config file does not exist.
            json.JSONDecodeError: If the config file is invalid JSON.
        """
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f'Configuration file not found: {config_path}')

        with open(path, 'r', encoding='utf-8') as f:
            raw: dict[str, Any] = json.load(f)

        # The bot has NO master/fallback trading account — every trade runs on a
        # per-user account whose credentials come from the database. The only
        # exchange access the container needs is keyless public market data.
        raw['api_key'] = ''
        raw['api_secret'] = ''

        # Platform infrastructure values from the environment (override config).
        raw['database_url'] = os.environ.get(
            'DATABASE_URL',
            raw.get('database_url', 'postgresql://zengrid:zengrid@localhost:5432/zengrid'),
        )
        raw['master_encryption_key'] = os.environ.get('MASTER_ENCRYPTION_KEY', '')
        raw['admin_api_key'] = os.environ.get('ADMIN_API_KEY', raw.get('admin_api_key', ''))
        raw['admin_api_port'] = int(
            os.environ.get('ADMIN_API_PORT', raw.get('admin_api_port', 8000))
        )

        settings = cls()
        for key, value in raw.items():
            if hasattr(settings, key):
                setattr(settings, key, value)

        # The master settings always run at the default leverage.
        settings.leverage = settings.clamp_leverage(settings.default_leverage)

        logger.info(
            'Settings loaded from %s (testnet=%s, symbols=%s, leverage=%dx)',
            config_path, settings.use_testnet,
            ','.join(s.split('/')[0] for s in settings.supported_symbols),
            settings.leverage,
        )
        return settings

    @classmethod
    def create_account_settings(
        cls, base_settings: 'Settings', overrides: dict
    ) -> 'Settings':
        """Create a per-account Settings instance.

        Every account uses the SAME strategy configuration. The ONLY per-account
        override honoured is the admin leverage override, clamped to the allowed
        admin range (3×–8×) and the hard ceiling (10×). Risk %, max positions,
        and TP/SL JSON overrides are intentionally IGNORED — sizing, limits, and
        targets are globally fixed so that no account can be scaled differently.

        Args:
            base_settings: The global Settings instance.
            overrides: Dict that may contain ``leverage_override`` (int or None).

        Returns:
            New Settings instance bound to the account's leverage.
        """
        account_settings = deepcopy(base_settings)

        lev_override = overrides.get('leverage_override')
        if lev_override is not None:
            # Admin override: allowed 3×–8×, never above the hard ceiling (10×).
            resolved = max(
                base_settings.min_leverage,
                min(int(lev_override), base_settings.max_leverage),
            )
            account_settings.leverage = account_settings.clamp_leverage(resolved)
        else:
            account_settings.leverage = account_settings.clamp_leverage(
                base_settings.default_leverage
            )

        return account_settings

    # ─────────────────────────────────────────
    # Instance Methods
    # ─────────────────────────────────────────

    def clamp_leverage(self, value: int) -> int:
        """Clamp a leverage value to the allowed range, never exceeding 10×."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = self.default_leverage
        return max(1, min(value, self.hard_max_leverage))

    def is_supported_symbol(self, symbol: str) -> bool:
        """True if the symbol is one of the three supported pairs."""
        return symbol in self.supported_symbols

    def get_layer_margin(self, layer_number: int) -> float:
        """FIXED margin (USDT) for a 1-based layer.

        Layer 1 = layer1_margin_usd. Layer 2 = 2× Layer 1 (the recovery layer).
        Sizing is identical for every account and never depends on balance.
        """
        if layer_number <= 1:
            return float(self.layer1_margin_usd)
        return float(self.layer1_margin_usd * self.layer2_margin_multiplier)

    def basket_tp_target_usd(self, layer_count: int) -> float:
        """Net USDT profit target that closes the whole basket.

        Layer 1 only      → basket_tp_layer1_usd   (≈ $0.50)
        Layer 1 + Layer 2 → basket_tp_recovery_usd (≈ $1.50–$2.00)
        """
        if layer_count >= 2:
            return float(self.basket_tp_recovery_usd)
        return float(self.basket_tp_layer1_usd)

    def validate(self) -> list[str]:
        """Validate settings and return a list of issues found (empty if OK)."""
        issues: list[str] = []

        if not self.supported_symbols:
            issues.append('supported_symbols must not be empty')
        if self.recovery_max_layers != 2:
            issues.append('recovery_max_layers must be exactly 2 (Layer 1 + one recovery)')
        if self.layer2_atr_multiplier <= 0:
            issues.append('layer2_atr_multiplier must be > 0')
        if self.default_leverage < self.min_leverage or self.default_leverage > self.max_leverage:
            issues.append('default_leverage must be within [min_leverage, max_leverage]')
        if self.max_leverage > self.hard_max_leverage:
            issues.append('max_leverage must not exceed hard_max_leverage (10)')
        if self.layer1_margin_usd <= 0:
            issues.append('layer1_margin_usd must be > 0')
        if self.layer2_margin_multiplier < 1:
            issues.append('layer2_margin_multiplier must be >= 1')
        if self.basket_tp_layer1_usd <= 0:
            issues.append('basket_tp_layer1_usd must be > 0')
        if self.basket_tp_recovery_usd <= self.basket_tp_layer1_usd:
            issues.append('basket_tp_recovery_usd should exceed basket_tp_layer1_usd')
        if self.daily_profit_target_usd <= 0:
            issues.append('daily_profit_target_usd must be > 0')
        if self.daily_loss_limit_usd <= 0:
            issues.append('daily_loss_limit_usd must be > 0')
        if self.max_baskets_per_account < 1:
            issues.append('max_baskets_per_account must be >= 1')
        if self.bb_period < 2:
            issues.append('bb_period must be >= 2')

        return issues
