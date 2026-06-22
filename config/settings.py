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

    # ── Supported symbols / watchlist (correlated assets — ONLY these traded) ──
    # Expanded to 10 liquid, low/mid-priced USDT-M perps for more opportunities.
    # Risk is unchanged: tier sizing, exposure caps, layer count, and per-account
    # position limits still bound everything — more symbols only means more
    # candidate setups, never more margin/exposure/layers.
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

    # ── Leverage (default 8x; admin may override 5x–10x; NEVER exceed 10x) ──
    # Fixed leverage — never dynamically adjusted, never balance-scaled.
    default_leverage: int = 8
    min_leverage: int = 5
    max_leverage: int = 10
    hard_max_leverage: int = 10
    # Resolved leverage actually used by an account (set per-account by
    # create_account_settings; defaults to default_leverage for the master).
    leverage: int = 8

    # ── Account tiers (FIXED sizing — balance ONLY selects the tier) ──
    # Exactly two tiers. Balance is evaluated solely to pick a tier; once a
    # basket is opened its tier is LOCKED (recovery margin, exposure cap, and TP
    # target all come from the basket's tier — never resized by later balance
    # changes such as deposits/withdrawals). There is no balance scaling, no
    # percentage sizing, no dynamic/adaptive/volatility sizing, no martingale.
    #
    #   Tier 1 ($20–$39.99) L1 $1 L2 $2 cap $3  daily +$2/−$3  4 sym/8 pos  death <$15
    #   Tier 2 ($40+)       L1 $2 L2 $4 cap $6  daily +$3.5/−$4 6 sym/12 pos death <$30
    # Rebalance: per-basket size halved and symbol/position caps doubled, so the
    # MAX total deployed margin is unchanged (Tier 1: 4×$3=$12; Tier 2: 6×$6=$36)
    # while diversification, trade frequency, and basket turnover rise.
    min_tier_balance: float = 20.0
    account_tiers: list = field(
        default_factory=lambda: [
            {
                'id': 'tier1', 'max_balance': 40.0,
                'layer1_margin': 1.0, 'layer2_margin': 2.0,
                'max_basket_exposure': 3.0,
                'basket_tp_l1': 0.30, 'basket_tp_l2': 0.80,
                # Recovery ROI normalized to 10% (was 12%) for faster recovery
                # exits; reward stays proportional to the $3 total margin.
                'layer1_roi_target': 0.12, 'recovery_roi_target': 0.10,
                'daily_profit_target': 2.0, 'daily_loss_limit': 3.0,
                'max_active_symbols': 4, 'max_positions': 8,
                'protection_floor': 15.0,
            },
            {
                'id': 'tier2', 'max_balance': float('inf'),
                'layer1_margin': 2.0, 'layer2_margin': 4.0,
                'max_basket_exposure': 6.0,
                'basket_tp_l1': 0.50, 'basket_tp_l2': 1.20,
                'layer1_roi_target': 0.10, 'recovery_roi_target': 0.10,
                'daily_profit_target': 3.5, 'daily_loss_limit': 4.0,
                'max_active_symbols': 6, 'max_positions': 12,
                'protection_floor': 30.0,
            },
        ]
    )
    # Absolute notional floor — Binance rejects dust orders below ~$5 notional.
    min_notional_floor: float = 5.0

    # ── Basket hard stop-loss (per-basket backstop, independent of daily limit) ──
    # A SINGLE basket must never consume a large slice of the daily loss
    # allowance. If a basket's NET PnL (gross − estimated round-trip fees) falls
    # to −basket_hard_sl_usd, the whole basket is closed immediately with reason
    # 'basket_sl'. This sits BELOW the account-level daily-loss/death-protection
    # guards (which still fire first when breached) and never weakens them — it
    # only adds an earlier, per-basket cut. Applies to Layer-1 AND recovery
    # baskets, every supported symbol.
    basket_hard_sl_usd: float = 0.30

    # ── Per-symbol ROI overrides (exit ROI target lookup) ──
    # The default ROI targets live on the tier (layer1_roi_target /
    # recovery_roi_target). A symbol listed here overrides those targets so a
    # capital-sticky symbol can be closed sooner. TRX historically stays open for
    # extended periods (capital locked, fees accruing), so it uses a tighter 8%
    # Layer-1 AND recovery ROI. Every other symbol keeps its tier values.
    symbol_roi_overrides: dict = field(
        default_factory=lambda: {
            'TRX/USDT:USDT': {'layer1_roi_target': 0.08, 'recovery_roi_target': 0.08},
        }
    )

    # ── Recovery model (max 2 layers — NO Layer 3/4/5, never a martingale) ──
    recovery_max_layers: int = 2
    # Layer 2 activates on a HYBRID trigger — whichever occurs FIRST of:
    #   A) price moves ATR(14) × layer2_atr_multiplier against Layer 1, OR
    #   B) Layer 1 floating loss ≥ recovery_loss_trigger_usd (USDT).
    # Volatility-adjusted spacing — NOT fixed grid spacing.
    layer2_atr_multiplier: float = 2.0
    recovery_loss_trigger_usd: float = 0.30

    # ── Position limits (max active symbols / total positions are PER-TIER) ──
    max_basket_per_symbol: int = 1          # never two baskets on one symbol

    # ── Correlation protection (TRX/XRP/XLM treated as correlated assets) ──
    # A new basket needs a minimum signal-strength score (0–4) that rises with
    # the number of already-open correlated baskets:
    #   0 active → score >= 2,   1+ active → score >= 3.
    correlation_min_score_first: int = 2
    correlation_min_score_additional: int = 3

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

        Used for managing EXISTING baskets / daily limits when the balance has
        dipped below the minimum tier — the tightest limits stay in force.
        """
        return self.get_tier(balance) or self.account_tiers[0]

    def get_tier_by_id(self, tier_id: Optional[str]) -> Optional[dict]:
        """Look up a tier by its stored id (used to read a basket's locked tier)."""
        for tier in self.account_tiers:
            if tier['id'] == tier_id:
                return tier
        return None

    def roi_targets_for(self, symbol: str, tier: dict) -> tuple:
        """Resolve (layer1_roi_target, recovery_roi_target) for a symbol.

        Starts from the basket's LOCKED tier values, then applies any
        per-symbol override from ``symbol_roi_overrides`` (e.g. TRX → 8%/8%).
        A symbol with no override keeps the tier defaults unchanged. Returns the
        targets as fractions (0–1).
        """
        l1 = float(tier.get('layer1_roi_target', 0.0))
        rec = float(tier.get('recovery_roi_target', 0.0))
        override = (self.symbol_roi_overrides or {}).get(symbol)
        if override:
            l1 = float(override.get('layer1_roi_target', l1))
            rec = float(override.get('recovery_roi_target', rec))
        return l1, rec

    def validate(self) -> list[str]:
        """Validate settings and return a list of issues found (empty if OK)."""
        issues: list[str] = []

        if not self.supported_symbols:
            issues.append('supported_symbols must not be empty')
        if len(self.account_tiers) != 2:
            issues.append('account_tiers must define exactly two tiers')
        if self.recovery_max_layers != 2:
            issues.append('recovery_max_layers must be exactly 2 (Layer 1 + one recovery)')
        if self.layer2_atr_multiplier <= 0:
            issues.append('layer2_atr_multiplier must be > 0')
        if self.default_leverage < self.min_leverage or self.default_leverage > self.max_leverage:
            issues.append('default_leverage must be within [min_leverage, max_leverage]')
        if self.max_leverage > self.hard_max_leverage:
            issues.append('max_leverage must not exceed hard_max_leverage (10)')
        for tier in self.account_tiers:
            tid = tier.get('id', '?')
            if tier.get('layer1_margin', 0) <= 0 or tier.get('layer2_margin', 0) <= 0:
                issues.append(f'{tid}: layer margins must be > 0')
            exposure = tier.get('layer1_margin', 0) + tier.get('layer2_margin', 0)
            if exposure > tier.get('max_basket_exposure', 0) + 1e-9:
                issues.append(f'{tid}: L1+L2 margin exceeds max_basket_exposure')
            if tier.get('basket_tp_l2', 0) <= tier.get('basket_tp_l1', 0):
                issues.append(f'{tid}: basket_tp_l2 must exceed basket_tp_l1')
            for roi_key in ('recovery_roi_target', 'layer1_roi_target'):
                roi = tier.get(roi_key, 0)
                if roi <= 0 or roi >= 1:
                    issues.append(f'{tid}: {roi_key} must be between 0 and 1')
            if tier.get('daily_profit_target', 0) <= 0 or tier.get('daily_loss_limit', 0) <= 0:
                issues.append(f'{tid}: daily targets must be > 0')
            if tier.get('max_active_symbols', 0) < 1:
                issues.append(f'{tid}: max_active_symbols must be >= 1')
            # Max positions should allow each symbol's basket to reach 2 layers.
            if tier.get('max_positions', 0) < tier.get('max_active_symbols', 0) * self.recovery_max_layers:
                issues.append(f'{tid}: max_positions must be >= max_active_symbols × max layers')
            if tier.get('protection_floor', 0) <= 0:
                issues.append(f'{tid}: protection_floor must be > 0')
            if tier.get('protection_floor', 0) >= tier.get('max_balance', float('inf')) and tier['max_balance'] != float('inf'):
                issues.append(f'{tid}: protection_floor should be below the tier ceiling')
            # Smaller per-layer margins must still clear the exchange dust floor:
            # Layer-1 notional = margin × leverage must be ≥ min_notional_floor,
            # else the smallest entry would be rejected as a dust order.
            l1_notional = tier.get('layer1_margin', 0) * self.default_leverage
            if l1_notional + 1e-9 < self.min_notional_floor:
                issues.append(
                    f'{tid}: layer1 notional {l1_notional:.2f} (margin×leverage) '
                    f'is below min_notional_floor {self.min_notional_floor:.2f}'
                )
        if self.recovery_loss_trigger_usd <= 0:
            issues.append('recovery_loss_trigger_usd must be > 0')
        if self.correlation_min_score_additional < self.correlation_min_score_first:
            issues.append('correlation_min_score_additional must be >= correlation_min_score_first')
        if self.bb_period < 2:
            issues.append('bb_period must be >= 2')
        if self.basket_hard_sl_usd <= 0:
            issues.append('basket_hard_sl_usd must be > 0')
        for sym, override in (self.symbol_roi_overrides or {}).items():
            for roi_key in ('layer1_roi_target', 'recovery_roi_target'):
                if roi_key in override:
                    roi = override[roi_key]
                    if roi <= 0 or roi >= 1:
                        issues.append(f'symbol_roi_overrides[{sym}].{roi_key} must be between 0 and 1')

        return issues
