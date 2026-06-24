"""
ZenGrid — Central Settings, Constants, and Enums.

Single-entry scalping core for Binance USDT-M Futures.

This module is the SINGLE source of truth for all strategy parameters. The
strategy is deliberately minimal and identical for every account:

  • Mean-reversion entries (RSI + Bollinger touch) on the 15m timeframe
  • A global BTC 15m trend filter gates trade direction
  • An ATR feasibility band keeps the fixed % stop out of noise and the fixed
    % target reachable in time
  • SINGLE ENTRY — exactly one position per symbol. NO recovery, NO Layer 2,
    NO averaging down, NO martingale, NO grid expansion.
  • Fixed take-profit and stop-loss expressed as a percentage of the position
    margin (TP 20% / SL 12%)
  • Per-account portfolio trailing profit lock (flatten on aggregate give-back)
  • FIXED position sizing — account balance never changes margin or position
    count (balance only selects the tier)
  • Per-account daily profit target and daily loss limit (per-tier)

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
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Fixed trading universe — exactly 100 USDT-M perpetual symbols.
# High liquidity, tight spreads, medium volatility. BTC and ETH are EXCLUDED
# from execution (BTC is the regime filter only; ETH's min-notional is too high
# for the tier margins). The bot scans ONLY these symbols and ignores every
# other Binance market. The startup universe validator drops any symbol the
# exchange reports as delisted/inactive — it never adds replacements.
# ─────────────────────────────────────────────

SUPPORTED_UNIVERSE: list = [
    # Core 20 — deepest liquidity, tightest spreads
    'SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT', 'DOGE/USDT:USDT',
    'ADA/USDT:USDT', 'AVAX/USDT:USDT', 'LINK/USDT:USDT', 'DOT/USDT:USDT',
    'TRX/USDT:USDT', 'LTC/USDT:USDT', 'BCH/USDT:USDT', 'NEAR/USDT:USDT',
    'SUI/USDT:USDT', 'TON/USDT:USDT', 'APT/USDT:USDT', 'ATOM/USDT:USDT',
    'UNI/USDT:USDT', 'FIL/USDT:USDT', 'ETC/USDT:USDT', 'XLM/USDT:USDT',
    # 21–50 — strong liquidity, medium volatility
    'ARB/USDT:USDT', 'OP/USDT:USDT', 'INJ/USDT:USDT', 'SEI/USDT:USDT',
    'TIA/USDT:USDT', 'AAVE/USDT:USDT', 'RUNE/USDT:USDT', 'FET/USDT:USDT',
    'RENDER/USDT:USDT', 'IMX/USDT:USDT', 'STX/USDT:USDT', 'LDO/USDT:USDT',
    'CRV/USDT:USDT', 'DYDX/USDT:USDT', 'GRT/USDT:USDT', 'ALGO/USDT:USDT',
    'ICP/USDT:USDT', 'HBAR/USDT:USDT', 'VET/USDT:USDT', 'POL/USDT:USDT',
    'SAND/USDT:USDT', 'MANA/USDT:USDT', 'AXS/USDT:USDT', 'GALA/USDT:USDT',
    'EGLD/USDT:USDT', 'THETA/USDT:USDT', 'FLOW/USDT:USDT', 'CHZ/USDT:USDT',
    'ENS/USDT:USDT', 'KSM/USDT:USDT',
    # 51–80 — established mid-caps
    'ENA/USDT:USDT', 'ONDO/USDT:USDT', 'JUP/USDT:USDT', 'PYTH/USDT:USDT',
    'JTO/USDT:USDT', 'WLD/USDT:USDT', 'BLUR/USDT:USDT', 'MASK/USDT:USDT',
    'COMP/USDT:USDT', 'SNX/USDT:USDT', 'SUSHI/USDT:USDT', '1INCH/USDT:USDT',
    'AR/USDT:USDT', 'ROSE/USDT:USDT', 'CELO/USDT:USDT', 'ANKR/USDT:USDT',
    'IOTA/USDT:USDT', 'QTUM/USDT:USDT', 'WAVES/USDT:USDT', 'NEO/USDT:USDT',
    'DASH/USDT:USDT', 'ZEC/USDT:USDT', 'BAT/USDT:USDT', 'ZRX/USDT:USDT',
    'YFI/USDT:USDT', 'BAL/USDT:USDT', 'GMT/USDT:USDT', 'APE/USDT:USDT',
    'CFX/USDT:USDT', 'ARKM/USDT:USDT',
    # 81–100 — liquid tail + capped high-volatility memecoins
    'JASMY/USDT:USDT', 'SKL/USDT:USDT', 'LRC/USDT:USDT', 'STORJ/USDT:USDT',
    'RSR/USDT:USDT', 'S/USDT:USDT', 'ORDI/USDT:USDT', '1000PEPE/USDT:USDT',
    'WIF/USDT:USDT', '1000BONK/USDT:USDT', '1000FLOKI/USDT:USDT',
    '1000SHIB/USDT:USDT', '1000SATS/USDT:USDT', 'MEME/USDT:USDT',
    'TURBO/USDT:USDT', 'PEOPLE/USDT:USDT', 'NEIRO/USDT:USDT', 'MEW/USDT:USDT',
    'FXS/USDT:USDT', 'GMX/USDT:USDT',
]


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

    # ── Supported symbols / fixed universe (ONLY these 100 are ever traded) ──
    supported_symbols: list = field(default_factory=lambda: list(SUPPORTED_UNIVERSE))
    # BTC is the trend-filter reference only — it is never traded (its margin
    # would be far below Binance's BTC min notional).
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

    # ── Leverage (default 10x; admin may override 8x–10x; NEVER exceed 10x) ──
    # Fixed leverage — never dynamically adjusted, never balance-scaled.
    default_leverage: int = 10
    min_leverage: int = 8
    max_leverage: int = 10
    hard_max_leverage: int = 10
    # Resolved leverage actually used by an account (set per-account by
    # create_account_settings; defaults to default_leverage for the master).
    leverage: int = 10

    # ── Account tiers (FIXED sizing — balance ONLY selects the tier) ──
    # Exactly two tiers. Balance is evaluated solely to pick a tier; once a
    # position is opened its tier is LOCKED (margin and limits come from the
    # position's tier — never resized by later balance changes such as
    # deposits/withdrawals). There is no balance scaling, no percentage sizing,
    # no dynamic/adaptive/volatility sizing, no martingale.
    #
    #   Tier 1 ($20–$39.99)  margin $0.8  8 positions   daily +$2/−$3   death <$15
    #                        portfolio lock: arm ≥$0.50, trail to max($0.35, peak×band%)
    #   Tier 2 ($40+)        margin $1.5  10 positions   daily +$3.5/−$4 death <$30
    #                        portfolio lock: arm ≥$0.80, trail to max($0.50, peak×band%)
    # SINGLE ENTRY: one position per symbol, so max_positions == max_active_symbols.
    min_tier_balance: float = 20.0
    account_tiers: list = field(
        default_factory=lambda: [
            {
                'id': 'tier1', 'max_balance': 40.0,
                'margin_per_trade': 0.8,
                'daily_profit_target': 2.0, 'daily_loss_limit': 3.0,
                'max_active_symbols': 8, 'max_positions': 8,
                'protection_floor': 15.0,
                # Portfolio trailing profit lock: arm at $0.50, then protect
                # max(floor $0.35, peak × band%). Bands are [peak_threshold, pct].
                'portfolio_lock_trigger': 0.50, 'portfolio_lock_floor': 0.35,
                'portfolio_protection_bands': [
                    [0.50, 0.70], [1.00, 0.75], [1.50, 0.80], [2.00, 0.85],
                ],
            },
            {
                'id': 'tier2', 'max_balance': float('inf'),
                'margin_per_trade': 1.5,
                'daily_profit_target': 3.5, 'daily_loss_limit': 4.0,
                'max_active_symbols': 10, 'max_positions': 10,
                'protection_floor': 30.0,
                'portfolio_lock_trigger': 0.80, 'portfolio_lock_floor': 0.50,
                'portfolio_protection_bands': [
                    [0.80, 0.70], [2.00, 0.75], [3.00, 0.80], [4.00, 0.85],
                ],
            },
        ]
    )
    # Absolute notional floor — Binance rejects dust orders below ~$5 notional.
    min_notional_floor: float = 5.0

    # ── Take-profit / stop-loss (fixed % of the position margin) ──
    # Both are evaluated on NET PnL (gross − round-trip fees). A position closes
    # on the FIRST of: net ≥ tp_margin_pct × margin (reason 'tp') or
    # net ≤ −sl_margin_pct × margin (reason 'sl').
    #   TP 20% → Tier 1 $0.16 / Tier 2 $0.30
    #   SL 12% → Tier 1 $0.096 / Tier 2 $0.18
    tp_margin_pct: float = 0.20
    sl_margin_pct: float = 0.12

    # ── ATR feasibility band (entry gate) ──
    # Skip an entry unless ATR(14)/price is inside this band, so the fixed % stop
    # is never inside pure noise (ATR too high) and the fixed % target stays
    # reachable in reasonable time (ATR too low). This is the "medium volatility"
    # market-selection criterion applied per-trade.
    atr_entry_min_pct: float = 0.003
    atr_entry_max_pct: float = 0.012

    # ── Signal quality gate ──
    # A new position requires a minimum signal-strength score (0–4): +1 extreme
    # RSI, +1 strong Bollinger penetration, +1 BTC trend aligned, +1 good spread
    # & liquidity.
    min_signal_score: int = 1

    # ── Position limits ──
    max_basket_per_symbol: int = 1          # never two positions on one symbol

    # ── Pre-trade risk-rule skip filters (skip the trade if ANY trips) ──
    risk_filter_lookback: int = 30          # bars for ATR/volume averages
    max_spread_pct: float = 0.0010          # spread / price ceiling (0.10%)
    atr_explosion_multiplier: float = 2.5   # current ATR > 2.5× average ATR
    news_candle_atr_multiplier: float = 2.5 # candle body > 2.5× ATR (news candle)
    volume_spike_multiplier: float = 3.0    # last volume > 3× average volume

    # ── Same-symbol cooldown after a position closes (per-account, persisted) ──
    # After a position closes on a symbol, NO new position on the SAME symbol for
    # this window; other symbols are unaffected (symbol-specific).
    symbol_cooldown_seconds: int = 1800     # 30 min

    # ── Fees (used for net-profit estimation on TP/SL) ──
    # Realistic Binance USDT-M taker fee (0.05%). Round-trip = 2× = 0.10% notional.
    taker_fee_pct: float = 0.0005

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

        # JSON cannot represent infinity — the top tier stores a large sentinel.
        for tier in raw.get('account_tiers', []):
            if tier.get('max_balance', 0) >= 999_999_999:
                tier['max_balance'] = float('inf')

        settings = cls()
        for key, value in raw.items():
            if hasattr(settings, key):
                setattr(settings, key, value)

        # The master settings always run at the default leverage.
        settings.leverage = settings.clamp_leverage(settings.default_leverage)

        logger.info(
            'Settings loaded from %s (testnet=%s, symbols=%d, leverage=%dx)',
            config_path, settings.use_testnet,
            len(settings.supported_symbols), settings.leverage,
        )
        return settings

    @classmethod
    def create_account_settings(
        cls, base_settings: 'Settings', overrides: dict
    ) -> 'Settings':
        """Create a per-account Settings instance.

        Every account uses the SAME strategy configuration. The ONLY per-account
        override honoured is the admin leverage override, clamped to the allowed
        admin range (8×–10×) and the hard ceiling (10×). Risk %, max positions,
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
            # Admin override: allowed 8×–10×, never above the hard ceiling (10×).
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
        """True if the symbol is one of the fixed supported pairs."""
        return symbol in self.supported_symbols

    # ── Account tier helpers (balance ONLY selects the tier) ──

    def get_tier(self, balance: float) -> Optional[dict]:
        """Return the tier config for a balance, or None if below the minimum.

        Balances below ``min_tier_balance`` ($20) have no tier and must not
        trade. Otherwise the first tier whose ``max_balance`` exceeds the
        balance wins (Tier 1 < $40, Tier 2 ≥ $40).
        """
        if balance < self.min_tier_balance:
            return None
        for tier in self.account_tiers:
            if balance < tier['max_balance']:
                return tier
        return self.account_tiers[-1]

    def get_tier_or_default(self, balance: float) -> dict:
        """Tier for a balance, falling back to the most conservative (Tier 1).

        Used for managing EXISTING positions / daily limits when the balance has
        dipped below the minimum tier — the tightest limits stay in force.
        """
        return self.get_tier(balance) or self.account_tiers[0]

    def get_tier_by_id(self, tier_id: Optional[str]) -> Optional[dict]:
        """Look up a tier by its stored id (used to read a position's locked tier)."""
        for tier in self.account_tiers:
            if tier['id'] == tier_id:
                return tier
        return None

    def validate(self) -> list[str]:
        """Validate settings and return a list of issues found (empty if OK)."""
        issues: list[str] = []

        if not self.supported_symbols:
            issues.append('supported_symbols must not be empty')
        if len(self.supported_symbols) != 100:
            issues.append(
                f'supported_symbols must define exactly 100 symbols '
                f'(found {len(self.supported_symbols)})'
            )
        if self.btc_symbol in self.supported_symbols:
            issues.append('btc_symbol must be the filter reference only, not a traded symbol')
        if len(self.account_tiers) != 2:
            issues.append('account_tiers must define exactly two tiers')
        if self.default_leverage < self.min_leverage or self.default_leverage > self.max_leverage:
            issues.append('default_leverage must be within [min_leverage, max_leverage]')
        if self.max_leverage > self.hard_max_leverage:
            issues.append('max_leverage must not exceed hard_max_leverage (10)')

        # Take-profit / stop-loss percentages.
        if not (0 < self.sl_margin_pct < 1):
            issues.append('sl_margin_pct must be between 0 and 1')
        if not (0 < self.tp_margin_pct < 1):
            issues.append('tp_margin_pct must be between 0 and 1')
        if self.tp_margin_pct <= self.sl_margin_pct:
            issues.append('tp_margin_pct should exceed sl_margin_pct for a positive R/R')

        # ATR feasibility band.
        if not (0 < self.atr_entry_min_pct < self.atr_entry_max_pct):
            issues.append('atr_entry_min_pct must be > 0 and < atr_entry_max_pct')
        if not (0 <= self.min_signal_score <= 4):
            issues.append('min_signal_score must be between 0 and 4')

        for tier in self.account_tiers:
            tid = tier.get('id', '?')
            if tier.get('margin_per_trade', 0) <= 0:
                issues.append(f'{tid}: margin_per_trade must be > 0')
            if tier.get('daily_profit_target', 0) <= 0 or tier.get('daily_loss_limit', 0) <= 0:
                issues.append(f'{tid}: daily targets must be > 0')
            if tier.get('max_active_symbols', 0) < 1:
                issues.append(f'{tid}: max_active_symbols must be >= 1')
            # Single entry: one position per symbol, so max_positions must at
            # least cover one position per active symbol.
            if tier.get('max_positions', 0) < tier.get('max_active_symbols', 0):
                issues.append(f'{tid}: max_positions must be >= max_active_symbols')
            if tier.get('protection_floor', 0) <= 0:
                issues.append(f'{tid}: protection_floor must be > 0')
            # Portfolio trailing profit lock: arm trigger must exceed the
            # minimum-protected floor, and both must be positive.
            trig = tier.get('portfolio_lock_trigger', 0)
            floor_lvl = tier.get('portfolio_lock_floor', 0)
            if trig <= 0 or floor_lvl <= 0:
                issues.append(f'{tid}: portfolio_lock_trigger/floor must be > 0')
            elif trig <= floor_lvl:
                issues.append(f'{tid}: portfolio_lock_trigger must exceed portfolio_lock_floor')
            # Dynamic protection bands [peak_threshold, pct]: non-empty, ascending
            # thresholds, pct in (0, 1], and the lowest band must be reachable at
            # the arm trigger (lowest threshold <= trigger).
            bands = tier.get('portfolio_protection_bands') or []
            if not bands:
                issues.append(f'{tid}: portfolio_protection_bands must not be empty')
            else:
                prev_thr = -1.0
                for band in bands:
                    if len(band) != 2:
                        issues.append(f'{tid}: each protection band must be [peak_threshold, pct]')
                        continue
                    thr, pct = float(band[0]), float(band[1])
                    if thr <= prev_thr:
                        issues.append(f'{tid}: protection band thresholds must strictly ascend')
                    if not (0 < pct <= 1):
                        issues.append(f'{tid}: protection band pct must be in (0, 1]')
                    prev_thr = thr
                if float(bands[0][0]) > trig + 1e-9:
                    issues.append(f'{tid}: lowest protection band threshold must be <= arm trigger')
            if tier.get('protection_floor', 0) >= tier.get('max_balance', float('inf')) and tier['max_balance'] != float('inf'):
                issues.append(f'{tid}: protection_floor should be below the tier ceiling')
            # The position notional = margin × leverage must clear the exchange
            # dust floor, else the order would be rejected.
            notional = tier.get('margin_per_trade', 0) * self.default_leverage
            if notional + 1e-9 < self.min_notional_floor:
                issues.append(
                    f'{tid}: position notional {notional:.2f} (margin×leverage) '
                    f'is below min_notional_floor {self.min_notional_floor:.2f}'
                )

        if self.bb_period < 2:
            issues.append('bb_period must be >= 2')

        return issues
