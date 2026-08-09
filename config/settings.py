"""
Zentry — Central Settings, Constants, and Enums.

Market-analysis configuration for the AI Trading Signal platform. This module is
the SINGLE source of truth for every analysis parameter:

  • Which pairs and timeframe are analysed
  • Indicator periods (RSI, Bollinger, ATR) and the EMA periods used by the
    BTC regime classifier
  • Market-quality filters (spread, ATR explosion, news candle, volume spike)
    that downgrade or reject a setup

The platform NEVER places, modifies, or closes an order. There are no exchange
credentials, no leverage, no position sizing, and no account tiers here — every
one of those existed only to execute trades automatically and was removed in the
Phase 0 conversion.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────

class Side(str, Enum):
    """Direction of an analysed setup."""
    LONG = 'long'
    SHORT = 'short'


class BtcRegime(str, Enum):
    """BTC trend regime used as market context for every analysed pair.

    BULLISH → BTC above 200 EMA and EMA50 above EMA200 → favours LONG
    BEARISH → BTC below 200 EMA and EMA50 below EMA200 → favours SHORT
    NEUTRAL → no clear trend                            → no directional bias
    UNKNOWN → BTC data unavailable (fail-safe)          → no directional bias
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

    # ── Market data (public, keyless — no credentials are ever used) ──
    use_testnet: bool = False

    # ── Database ──
    database_url: str = 'postgresql://zengrid:zengrid@localhost:5432/zengrid'

    # ── Pairs available for analysis ──
    supported_symbols: list = field(
        default_factory=lambda: [
            'TRX/USDT:USDT',
            'XRP/USDT:USDT',
            'XLM/USDT:USDT',
            'ADA/USDT:USDT',
            'ALGO/USDT:USDT',
            'HBAR/USDT:USDT',
            'VET/USDT:USDT',
            'LINK/USDT:USDT',
            'DOT/USDT:USDT',
            'ATOM/USDT:USDT',
            'LTC/USDT:USDT',
            'POL/USDT:USDT',
            'ETC/USDT:USDT',
            'BCH/USDT:USDT',
            'NEAR/USDT:USDT',
            'EOS/USDT:USDT',
            'FIL/USDT:USDT',
            'IOTA/USDT:USDT',
            'GRT/USDT:USDT',
            'AVAX/USDT:USDT',
        ]
    )
    btc_symbol: str = 'BTC/USDT:USDT'

    # ── Timeframe ──
    # The pair/timeframe a user selects on the website overrides these; they are
    # the defaults used when a request does not specify one.
    timeframe: str = '15m'
    candle_limit: int = 300

    # ── Indicators ──
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14

    # ── BTC trend context (EMA50 / EMA200) ──
    btc_filter_enabled: bool = True
    btc_ema_fast: int = 50
    btc_ema_slow: int = 200
    # Cache the computed BTC regime for this many seconds so analysing several
    # pairs in quick succession does not refetch BTC candles each time.
    btc_regime_cache_seconds: int = 300

    # ── Market-quality filters (a setup is rejected if ANY trips) ──
    risk_filter_lookback: int = 30          # bars for ATR/volume averages
    max_spread_pct: float = 0.0010          # spread / price ceiling (0.10%)
    atr_explosion_multiplier: float = 2.5   # current ATR > 2.5× average ATR
    news_candle_atr_multiplier: float = 2.5 # candle body > 2.5× ATR (news candle)
    volume_spike_multiplier: float = 3.0    # last volume > 3× average volume

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

        # Platform infrastructure values from the environment (override config).
        raw['database_url'] = os.environ.get(
            'DATABASE_URL',
            raw.get('database_url', 'postgresql://zengrid:zengrid@localhost:5432/zengrid'),
        )

        settings = cls()
        for key, value in raw.items():
            if hasattr(settings, key):
                setattr(settings, key, value)

        logger.info(
            'Settings loaded from %s (timeframe=%s, symbols=%s)',
            config_path, settings.timeframe,
            ','.join(s.split('/')[0] for s in settings.supported_symbols),
        )
        return settings

    # ─────────────────────────────────────────
    # Instance Methods
    # ─────────────────────────────────────────

    def is_supported_symbol(self, symbol: str) -> bool:
        """True if the symbol is one of the pairs available for analysis."""
        return symbol in self.supported_symbols

    def validate(self) -> list[str]:
        """Validate settings and return a list of issues found (empty if OK)."""
        issues: list[str] = []

        if not self.supported_symbols:
            issues.append('supported_symbols must not be empty')
        if not self.btc_symbol:
            issues.append('btc_symbol must not be empty')
        if not self.timeframe:
            issues.append('timeframe must not be empty')
        if self.candle_limit < 2:
            issues.append('candle_limit must be >= 2')
        if self.rsi_period < 2:
            issues.append('rsi_period must be >= 2')
        if not 0 < self.rsi_oversold < self.rsi_overbought < 100:
            issues.append('require 0 < rsi_oversold < rsi_overbought < 100')
        if self.bb_period < 2:
            issues.append('bb_period must be >= 2')
        if self.bb_std <= 0:
            issues.append('bb_std must be > 0')
        if self.atr_period < 2:
            issues.append('atr_period must be >= 2')
        if self.btc_ema_fast >= self.btc_ema_slow:
            issues.append('btc_ema_fast must be < btc_ema_slow')
        if self.btc_regime_cache_seconds < 0:
            issues.append('btc_regime_cache_seconds must be >= 0')
        if self.risk_filter_lookback < 5:
            issues.append('risk_filter_lookback must be >= 5')
        if self.max_spread_pct <= 0:
            issues.append('max_spread_pct must be > 0')
        for name in (
            'atr_explosion_multiplier',
            'news_candle_atr_multiplier',
            'volume_spike_multiplier',
        ):
            if getattr(self, name) <= 0:
                issues.append(f'{name} must be > 0')

        return issues
