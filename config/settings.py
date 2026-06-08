"""
ZenGrid — Central Settings, Constants, and Enums.

Loads configuration from config.json and provides typed access to all
trading parameters. All magic numbers are centralized here.

Extended for multi-account support: DATABASE_URL, MASTER_ENCRYPTION_KEY,
admin API settings, and per-account settings overrides.
"""

import json
import logging
import os
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────

class MarketRegime(str, Enum):
    """Detected market structure."""
    TRENDING = 'trending'
    SIDEWAYS = 'sideways'
    UNKNOWN = 'unknown'


class VolatilityLevel(str, Enum):
    """Current volatility classification."""
    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'


class Side(str, Enum):
    """Trade direction."""
    LONG = 'long'
    SHORT = 'short'


# ─────────────────────────────────────────────
# Settings Dataclass
# ─────────────────────────────────────────────

@dataclass
class Settings:
    """Central configuration container loaded from config.json.

    Extended with multi-account platform settings.
    """

    # ── API (master account, from environment) ──
    api_key: str = ''
    api_secret: str = ''
    use_testnet: bool = True

    # ── Database ──
    database_url: str = 'postgresql://zengrid:zengrid@localhost:5432/zengrid'

    # ── Encryption ──
    master_encryption_key: str = ''

    # ── Admin API ──
    admin_api_key: str = ''
    admin_api_port: int = 8000

    # ── Timing ──
    scan_interval_seconds: int = 600
    signal_timeframe: str = '5m'
    trend_timeframe: str = '1h'
    loop_interval_seconds: int = 10

    # ── Indicators ──
    rsi_period: int = 14
    ema_period: int = 200
    adx_period: int = 14
    atr_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    adx_trend_threshold: float = 25.0
    adx_sideways_threshold: float = 20.0
    high_vol_atr_multiplier: float = 1.5
    low_vol_atr_multiplier: float = 0.7

    # ── Scanner ──
    max_watchlist_size: int = 20
    min_volume_24h: float = 50_000_000.0
    max_funding_rate: float = 0.001
    min_coin_age_days: int = 30

    # ── Recovery System ──
    recovery_max_layers: int = 4
    recovery_margin_multipliers: list = field(
        default_factory=lambda: [1.0, 1.33, 1.67, 2.17]
    )
    recovery_atr_distances: list = field(
        default_factory=lambda: [0.0, 0.75, 1.0, 1.25]
    )

    # ── Take Profit ──
    basket_tp_roi: dict = field(
        default_factory=lambda: {'low': 0.08, 'medium': 0.12, 'high': 0.15}
    )
    individual_tp_atr_mult: float = 2.0

    # ── Stop Loss ──
    individual_sl_atr_mult: float = 3.0
    basket_sl_pct: float = 0.20
    emergency_sl_account_pct: float = 0.03

    # ── Risk Management ──
    daily_loss_limit_pct: float = 0.05
    max_exposure_pct: float = 0.25
    max_drawdown_pct: float = 0.15

    # ── Leverage ──
    leverage_by_volatility: dict = field(
        default_factory=lambda: {'low': 10, 'medium': 8, 'high': 5}
    )

    # ── Position Sizing Tiers ──
    position_margin_tiers: list = field(
        default_factory=lambda: [
            {'max_balance': 50, 'margin_range': [0.40, 0.60], 'max_positions': 3},
            {'max_balance': 100, 'margin_range': [0.60, 0.80], 'max_positions': 5},
            {'max_balance': float('inf'), 'margin_range': [0.90, 1.50], 'max_positions': 8},
        ]
    )

    # ── Fees & Slippage ──
    slippage_pct: float = 0.0005
    taker_fee_pct: float = 0.0004
    maker_fee_pct: float = 0.0004

    # ── Logging ──
    log_level: str = 'INFO'

    # ─────────────────────────────────────────
    # Class Methods
    # ─────────────────────────────────────────

    @classmethod
    def load(cls, config_path: str = 'config/config.json') -> 'Settings':
        """Load settings from a JSON configuration file.

        Args:
            config_path: Path to the JSON config file.

        Returns:
            Fully populated Settings instance.

        Raises:
            FileNotFoundError: If config file does not exist.
            json.JSONDecodeError: If config file is invalid JSON.
        """
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f'Configuration file not found: {config_path}')

        with open(path, 'r', encoding='utf-8') as f:
            raw: dict[str, Any] = json.load(f)

        # Override from environment variables if present
        raw['api_key'] = os.environ.get('BINANCE_API_KEY', raw.get('api_key', ''))
        raw['api_secret'] = os.environ.get('BINANCE_API_SECRET', raw.get('api_secret', ''))

        # Database URL from env or config
        raw['database_url'] = os.environ.get(
            'DATABASE_URL',
            raw.get('database_url', 'postgresql://zengrid:zengrid@localhost:5432/zengrid'),
        )

        # Encryption key from env
        raw['master_encryption_key'] = os.environ.get('MASTER_ENCRYPTION_KEY', '')

        # Admin API settings from env
        raw['admin_api_key'] = os.environ.get('ADMIN_API_KEY', raw.get('admin_api_key', ''))
        raw['admin_api_port'] = int(
            os.environ.get('ADMIN_API_PORT', raw.get('admin_api_port', 8000))
        )

        # Handle inf serialisation from JSON (stored as large number)
        tiers = raw.get('position_margin_tiers', [])
        for tier in tiers:
            if tier.get('max_balance', 0) >= 999_999_999:
                tier['max_balance'] = float('inf')

        settings = cls()
        for key, value in raw.items():
            if hasattr(settings, key):
                setattr(settings, key, value)

        logger.info(
            'Settings loaded from %s (testnet=%s)', config_path, settings.use_testnet
        )
        return settings

    @classmethod
    def create_account_settings(
        cls, base_settings: 'Settings', overrides: dict
    ) -> 'Settings':
        """Create a per-account Settings instance by merging overrides.

        This allows each account to have custom risk_pct, max_positions,
        leverage, TP/SL settings while inheriting all other global settings.

        Args:
            base_settings: The global Settings instance.
            overrides: Dict of account-specific overrides. Supported keys:
                - risk_pct: float (overrides daily_loss_limit_pct)
                - max_positions: int (applied as cap across all tiers)
                - leverage_override: int (overrides all volatility-based leverage)
                - tp_settings: dict (merged into basket_tp_roi, individual_tp_atr_mult)
                - sl_settings: dict (merged into basket_sl_pct, individual_sl_atr_mult, etc.)

        Returns:
            New Settings instance with account-specific values.
        """
        account_settings = deepcopy(base_settings)

        # Risk override
        risk_pct = overrides.get('risk_pct')
        if risk_pct is not None:
            account_settings.daily_loss_limit_pct = risk_pct

        # Max positions override — cap all tiers
        max_pos = overrides.get('max_positions')
        if max_pos is not None:
            for tier in account_settings.position_margin_tiers:
                tier['max_positions'] = min(tier['max_positions'], max_pos)

        # Leverage override — use fixed leverage for all volatility levels
        leverage = overrides.get('leverage_override')
        if leverage is not None:
            account_settings.leverage_by_volatility = {
                'low': leverage,
                'medium': leverage,
                'high': leverage,
            }

        # TP settings override
        tp = overrides.get('tp_settings')
        if tp and isinstance(tp, dict):
            if 'basket_tp_roi' in tp:
                account_settings.basket_tp_roi.update(tp['basket_tp_roi'])
            if 'individual_tp_atr_mult' in tp:
                account_settings.individual_tp_atr_mult = tp['individual_tp_atr_mult']

        # SL settings override
        sl = overrides.get('sl_settings')
        if sl and isinstance(sl, dict):
            if 'basket_sl_pct' in sl:
                account_settings.basket_sl_pct = sl['basket_sl_pct']
            if 'individual_sl_atr_mult' in sl:
                account_settings.individual_sl_atr_mult = sl['individual_sl_atr_mult']
            if 'emergency_sl_account_pct' in sl:
                account_settings.emergency_sl_account_pct = sl['emergency_sl_account_pct']

        return account_settings

    # ─────────────────────────────────────────
    # Instance Methods
    # ─────────────────────────────────────────

    def get_tier(self, balance: float) -> dict:
        """Return the position-sizing tier matching the current balance.

        Args:
            balance: Current account balance in USDT.

        Returns:
            Tier dict with keys: max_balance, margin_range, max_positions.
        """
        for tier in self.position_margin_tiers:
            if balance < tier['max_balance']:
                return tier
        # Fallback to last tier
        return self.position_margin_tiers[-1]

    def get_max_positions(self, balance: float) -> int:
        """Maximum simultaneous positions allowed for the given balance.

        Args:
            balance: Current account balance in USDT.

        Returns:
            Integer position limit.
        """
        return self.get_tier(balance)['max_positions']

    def get_base_margin_range(self, balance: float) -> tuple[float, float]:
        """Return (min_margin, max_margin) for the balance tier.

        Args:
            balance: Current account balance in USDT.

        Returns:
            Tuple of (minimum_margin, maximum_margin) in USDT.
        """
        tier = self.get_tier(balance)
        mr = tier['margin_range']
        return (mr[0], mr[1])

    def get_leverage(self, volatility: VolatilityLevel) -> int:
        """Lookup dynamic leverage based on volatility.

        Args:
            volatility: Current VolatilityLevel classification.

        Returns:
            Leverage multiplier (5, 8, or 10).
        """
        return self.leverage_by_volatility.get(volatility.value, 8)

    def get_basket_tp_roi(self, volatility: VolatilityLevel) -> float:
        """Basket take-profit ROI target by volatility.

        Args:
            volatility: Current VolatilityLevel classification.

        Returns:
            ROI target as decimal (e.g. 0.08 = 8%).
        """
        return self.basket_tp_roi.get(volatility.value, 0.12)

    def validate(self) -> list[str]:
        """Validate settings and return list of issues found.

        Returns:
            List of validation error messages. Empty if all OK.
        """
        issues: list[str] = []

        if not self.use_testnet and not self.api_key:
            issues.append('api_key is required for live trading')
        if not self.use_testnet and not self.api_secret:
            issues.append('api_secret is required for live trading')
        if self.recovery_max_layers < 1 or self.recovery_max_layers > 10:
            issues.append('recovery_max_layers must be between 1 and 10')
        if len(self.recovery_margin_multipliers) != self.recovery_max_layers:
            issues.append('recovery_margin_multipliers length must match recovery_max_layers')
        if len(self.recovery_atr_distances) != self.recovery_max_layers:
            issues.append('recovery_atr_distances length must match recovery_max_layers')
        if self.daily_loss_limit_pct <= 0 or self.daily_loss_limit_pct >= 1:
            issues.append('daily_loss_limit_pct must be between 0 and 1')
        if self.max_exposure_pct <= 0 or self.max_exposure_pct >= 1:
            issues.append('max_exposure_pct must be between 0 and 1')
        if self.max_drawdown_pct <= 0 or self.max_drawdown_pct >= 1:
            issues.append('max_drawdown_pct must be between 0 and 1')

        return issues
