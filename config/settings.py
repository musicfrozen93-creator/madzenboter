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


class BtcRegime(str, Enum):
    """BTC market-direction regime used as a global trade filter.

    UP_IMPULSE   → BTC in a strong uptrend   (allow LONG, block SHORT)
    DOWN_IMPULSE → BTC in a strong downtrend (allow SHORT, block LONG)
    SIDEWAYS     → no strong impulse         (allow both LONG and SHORT)
    UNKNOWN      → BTC data unavailable       (fail-safe: allow both)
    """
    UP_IMPULSE = 'up_impulse'
    DOWN_IMPULSE = 'down_impulse'
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
    # How often NEW-entry signal generation runs. Exit management runs every
    # loop_interval_seconds REGARDLESS of this value; throttling only the
    # (slower) signal phase keeps the exit cycle on the tight loop cadence so
    # open positions are never delayed behind scanning for new opportunities.
    # Entries are driven by 5m candles, so a 30s re-evaluation loses no setups.
    signal_eval_interval_seconds: int = 30

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

    # ── Entry signal thresholds (configurable; control trade frequency) ──
    # LONG  enters when RSI < rsi_long_threshold (and the trend filter passes).
    # SHORT enters when RSI > rsi_short_threshold (and the trend filter passes).
    # Higher long / lower short = MORE frequent trades. Defaults 40/60 are a
    # commercial balance between frequency and avoiding random-noise entries.
    rsi_long_threshold: float = 35.0
    rsi_short_threshold: float = 65.0
    # When True (default) the EMA200 higher-timeframe trend filter is mandatory:
    # only long above EMA200, only short below. Set False to trade on RSI alone
    # (NOT recommended — removes trend protection for the averaging grid).
    require_ema_trend_filter: bool = True

    # ── BTC Regime Filter (market-direction gate) ──
    # When enabled, BTC's higher-timeframe regime gates the direction of every
    # signal: UP_IMPULSE allows only LONG, DOWN_IMPULSE allows only SHORT,
    # SIDEWAYS allows both. Fail-safe: if BTC data is unavailable, both allowed.
    btc_regime_filter_enabled: bool = True
    btc_symbol: str = 'BTC/USDT:USDT'
    # Cache the computed BTC regime for this many seconds to avoid refetching
    # BTC candles for every symbol evaluated in a loop.
    btc_regime_cache_seconds: int = 300

    # ── Scanner ──
    max_watchlist_size: int = 50
    min_volume_24h: float = 50_000_000.0
    max_funding_rate: float = 0.001
    min_coin_age_days: int = 30
    # Composite-score boost applied to preferred small-account symbols so they
    # rank into the watchlist (0 = no boost). Keeps liquid, low-priced pairs in
    # rotation for $20–$100 accounts.
    preferred_symbol_score_boost: float = 0.15

    # ── Recovery System (2-layer: initial entry + ONE recovery layer) ──
    # Layer 1 = initial entry, Layer 2 = the single recovery layer. There is no
    # third or fourth layer: the maximum number of layers per basket is 2. This
    # bounds the size a losing basket can grow to while keeping one averaging
    # layer of recovery capability.
    recovery_max_layers: int = 2
    recovery_margin_multipliers: list = field(
        default_factory=lambda: [1.0, 1.0]
    )
    # Layer 2 triggers at 0.75 × ATR below/above the Layer-1 entry (existing
    # spacing for the first recovery layer is preserved unchanged).
    recovery_atr_distances: list = field(
        default_factory=lambda: [0.0, 0.75]
    )

    # ── Take Profit & Profit Protection ──
    # Every basket targets a fixed 15% ROI take-profit. Volatility no longer
    # varies the target (kept here only for backward-compat callers).
    basket_tp_roi: dict = field(
        default_factory=lambda: {'low': 0.15, 'medium': 0.15, 'high': 0.15}
    )
    # Fixed basket TP target (ROI as a decimal). Closes the basket outright.
    basket_tp_target_roi: float = 0.15
    # Profit-protection trailing: once basket ROI reaches the ARM level the
    # protection is armed (sticky, survives restart). If an armed basket's ROI
    # then falls back to the FLOOR level, the basket is closed immediately to
    # lock in profit. Prevents winners turning into losers on reversals.
    profit_protection_arm_roi: float = 0.10
    profit_protection_floor_roi: float = 0.08
    individual_tp_atr_mult: float = 2.0

    # ── Stop Loss ──
    individual_sl_atr_mult: float = 3.0
    basket_sl_pct: float = 0.15
    emergency_sl_account_pct: float = 0.03

    # ── Same-Symbol Cooldown ──
    # After a basket closes (for ANY reason) the symbol enters a cooldown during
    # which no new basket may be opened on it. Per-account, persisted in the DB
    # state store so it survives restarts. Default 30 minutes.
    symbol_cooldown_seconds: int = 1800

    # ── Risk Management ──
    daily_loss_limit_pct: float = 0.05
    max_exposure_pct: float = 0.25
    max_drawdown_pct: float = 0.15

    # ── Account-Size-Aware Daily Drawdown ──
    # Daily drawdown (from the day's starting balance) that pauses NEW entries
    # until the next UTC day. This is a ROUTINE limit: it auto-resets daily and
    # never triggers a permanent shutdown. First tier whose max_balance >= balance
    # wins. Smaller accounts get more room; larger accounts are protected tighter.
    daily_drawdown_tiers: list = field(
        default_factory=lambda: [
            {'max_balance': 50, 'drawdown_pct': 0.15},     # <= $50  → 15%
            {'max_balance': 200, 'drawdown_pct': 0.10},    # $50–200 → 10%
            {'max_balance': float('inf'), 'drawdown_pct': 0.05},  # > $200 → 5%
        ]
    )
    # CATASTROPHIC all-time drawdown from the high-water mark. ONLY this (a sign
    # of a genuine system/logic failure) triggers a permanent emergency shutdown
    # requiring manual review. Routine daily drawdown does NOT.
    catastrophic_drawdown_pct: float = 0.50

    # ── Daily Profit Trailing Lock (per-account, persisted) ──
    # Protects realized daily gains. Daily gain is measured from the day's
    # starting balance. As gain crosses each tier's `gain` level a profit `floor`
    # is ARMED; the floor only ratchets UP, never down. If daily gain later falls
    # back to the armed floor, NEW entries stop for the rest of the UTC day. At
    # the hard-stop level NEW entries stop immediately. Existing positions keep
    # being managed and closed. State is per-account and resets each UTC day.
    #
    #   gain 8%  → floor 5%
    #   gain 10% → floor 8%
    #   gain 12% → floor 10%
    #   gain 15% → immediate hard stop (no new entries the rest of the day)
    daily_profit_lock_tiers: list = field(
        default_factory=lambda: [
            {'gain': 0.08, 'floor': 0.05},
            {'gain': 0.10, 'floor': 0.08},
            {'gain': 0.12, 'floor': 0.10},
        ]
    )
    daily_profit_hard_stop_pct: float = 0.15

    # ── Loss-Streak Pause (per-account, persisted) ──
    # After this many CONSECUTIVE losing baskets, pause NEW entries for
    # loss_streak_pause_seconds. Any winning/break-even basket resets the streak.
    # Existing positions keep being managed; the pause auto-expires and survives
    # restart (stored in the DB state store like other persistent protections).
    loss_streak_threshold: int = 3
    loss_streak_pause_seconds: int = 3600

    # ── Account-Size-Aware Margin Caps ──
    # Hard ceiling on the TOTAL margin a single basket (all recovery layers
    # combined) may consume, as a fraction of account balance.
    # $20 → $2.00, $50 → $5.00, $100 → $10.00, $500 → $50.00.
    margin_hard_cap_pct: float = 0.10
    # Target margin for the FIRST entry layer, as a fraction of balance.
    # Volatility picks within this range (HIGH→low end, LOW→high end).
    # $20 → $0.50–$1.00, $50 → $1.25–$2.50, $100 → $2.50–$5.00.
    margin_target_pct_range: list = field(default_factory=lambda: [0.025, 0.05])
    # Absolute floor — Binance rejects dust orders below this margin.
    min_margin_floor: float = 0.30
    # Minimum estimated distance-to-liquidation required to open a position.
    # Rejects entries whose leverage would place liquidation too close.
    min_liquidation_distance_pct: float = 0.04
    # Maintenance-margin rate assumption used for the liquidation-distance estimate.
    maintenance_margin_rate: float = 0.005

    # ── Market Selection (account-size aware) ──
    # Accounts at or below this balance are treated as "small" and prefer the
    # liquid, lower-priced symbols below (finer lot steps → margin fits the cap).
    small_account_threshold: float = 100.0
    preferred_small_account_symbols: list = field(
        default_factory=lambda: [
            'DOGE/USDT', 'XRP/USDT', 'ADA/USDT', 'TRX/USDT', 'SUI/USDT',
            'LINK/USDT', 'ATOM/USDT', 'AVAX/USDT', 'ETC/USDT', 'BCH/USDT',
            'FET/USDT', 'INJ/USDT', 'NEAR/USDT', 'HBAR/USDT', 'ALGO/USDT',
            'VET/USDT', 'FIL/USDT', 'APT/USDT', 'ARB/USDT', 'OP/USDT',
        ]
    )

    # ── Leverage ──
    leverage_by_volatility: dict = field(
        default_factory=lambda: {'low': 10, 'medium': 8, 'high': 5}
    )

    # ── Position Sizing (balance-tier fixed basket sizing) ──
    # Baskets are NOT sized as a percentage of balance. Each account falls into a
    # balance TIER with FIXED absolute per-layer margins (USDT) and a FIXED total
    # basket-margin cap. Layer 1 = initial entry, Layer 2 = the single recovery
    # layer; their sum equals (and never exceeds) the tier's max_basket cap.
    #
    #   Tier A   $10–$50    L1 $1.50   L2 $1.00   max basket $2.50
    #   Tier B   $50–$200   L1 $2.50   L2 $1.00   max basket $3.50
    #   Tier C   > $200     L1 $3.50   L2 $1.00   max basket $4.50
    #
    # First tier whose max_balance >= balance wins (so $50 → A, $200 → B).
    max_positions: int = 8
    basket_sizing_tiers: list = field(
        default_factory=lambda: [
            {'max_balance': 50, 'layer1': 1.50, 'layer2': 1.00, 'max_basket': 2.50},
            {'max_balance': 200, 'layer1': 2.50, 'layer2': 1.00, 'max_basket': 3.50},
            {'max_balance': float('inf'), 'layer1': 3.50, 'layer2': 1.00, 'max_basket': 4.50},
        ]
    )
    # Absolute global ceiling — no basket may EVER exceed this across any tier.
    # Equals the largest tier cap (Tier C, $4.50). Backstop for legacy callers.
    max_basket_margin_usd: float = 4.5
    # Legacy fixed per-layer distribution, used ONLY when no balance is supplied
    # to get_layer_margin(); superseded by basket_sizing_tiers when balance known.
    basket_layer_margins_usd: list = field(
        default_factory=lambda: [2.0, 1.0]
    )

    # ── Position Sizing Tiers (legacy; max_positions now fixed via `max_positions`) ──
    position_margin_tiers: list = field(
        default_factory=lambda: [
            {'max_balance': 50, 'margin_range': [0.40, 0.60], 'max_positions': 8},
            {'max_balance': 100, 'margin_range': [0.60, 0.80], 'max_positions': 8},
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

        # NOTE: BINANCE_API_KEY / BINANCE_API_SECRET are intentionally NOT read.
        # The bot has no master/fallback trading account — every trade runs on a
        # per-user account whose credentials come from the database. The only
        # exchange access the container needs is keyless public market data.
        raw['api_key'] = ''
        raw['api_secret'] = ''

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
        for tier in raw.get('position_margin_tiers', []):
            if tier.get('max_balance', 0) >= 999_999_999:
                tier['max_balance'] = float('inf')
        for tier in raw.get('daily_drawdown_tiers', []):
            if tier.get('max_balance', 0) >= 999_999_999:
                tier['max_balance'] = float('inf')
        for tier in raw.get('basket_sizing_tiers', []):
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

        Globally enforced (NOT overridable per account):
            - max_positions   → always the global value (8)
            - basket_sl_pct   → always the global value (15%)
            - basket_tp_target_roi / profit_protection_arm_roi /
              profit_protection_floor_roi → not in the override path at all
              (basket TP 15% and profit protection 10%→8% are global for all).

        Args:
            base_settings: The global Settings instance.
            overrides: Dict of account-specific overrides. Supported keys:
                - risk_pct: float (overrides daily_loss_limit_pct)
                - max_positions: int (IGNORED — max positions is globally fixed)
                - leverage_override: int (overrides all volatility-based leverage)
                - tp_settings: dict (individual_tp_atr_mult only; basket TP is global)
                - sl_settings: dict (individual_sl_atr_mult, emergency_sl_account_pct;
                  basket_sl_pct is IGNORED — basket SL is globally fixed at 15%)

        Returns:
            New Settings instance with account-specific values.
        """
        account_settings = deepcopy(base_settings)

        # Risk override
        risk_pct = overrides.get('risk_pct')
        if risk_pct is not None:
            account_settings.daily_loss_limit_pct = risk_pct

        # Max positions — INTENTIONALLY NOT overridden per account. Every account
        # uses the same fixed cap (Settings.max_positions, default 8) for
        # consistent participation. Per-account max_positions overrides from the
        # database are deliberately ignored here.

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
            # basket_sl_pct is INTENTIONALLY NOT overridable per account. The
            # basket stop-loss is force-fixed to the global value (15%) for every
            # account — exactly like max_positions — so no legacy account JSON can
            # bypass it. Any `basket_sl_pct` key in sl_settings is ignored here.
            if 'basket_sl_pct' in sl:
                logger.warning(
                    'Ignoring account basket_sl_pct override (%s): basket SL is '
                    'globally enforced at %.0f%%.',
                    sl['basket_sl_pct'], account_settings.basket_sl_pct * 100,
                )
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
        """Maximum simultaneous positions — fixed for every account.

        Balance-tier position limits have been removed: all accounts use the
        same cap (`max_positions`, default 8) regardless of balance.

        Args:
            balance: Current account balance in USDT (ignored; kept for API).

        Returns:
            Integer position limit (the fixed account-wide cap).
        """
        return self.max_positions

    def get_base_margin_range(self, balance: float) -> tuple[float, float]:
        """Return (min_margin, max_margin) for the balance tier.

        Legacy absolute-dollar tier range. Superseded for sizing by
        get_target_margin_range() (percentage-based, account-size aware) but
        retained for backward compatibility.

        Args:
            balance: Current account balance in USDT.

        Returns:
            Tuple of (minimum_margin, maximum_margin) in USDT.
        """
        tier = self.get_tier(balance)
        mr = tier['margin_range']
        return (mr[0], mr[1])

    # ── Account-Size-Aware Margin & Drawdown helpers ──

    def get_basket_sizing_tier(self, balance: float) -> dict:
        """Return the balance-tier sizing config for the given balance.

        First tier whose max_balance >= balance wins (Tier A <= $50,
        Tier B <= $200, Tier C otherwise). Each tier dict carries
        `layer1`, `layer2`, and `max_basket` absolute USDT margins.
        """
        for tier in self.basket_sizing_tiers:
            if balance <= tier['max_balance']:
                return tier
        return self.basket_sizing_tiers[-1]

    def get_margin_hard_cap(self, balance: float) -> float:
        """Hard ceiling on TOTAL margin per basket (all layers combined).

        Balance-tier based: $2.50 (Tier A, <= $50), $3.50 (Tier B, <= $200),
        $4.50 (Tier C, > $200). Clamped to the absolute global ceiling
        (`max_basket_margin_usd`, $4.50). The recovery cap in the position
        manager enforces this across Layer 1 + Layer 2 so a basket can never
        exceed its tier maximum.
        """
        cap = self.get_basket_sizing_tier(balance)['max_basket']
        return min(cap, self.max_basket_margin_usd)

    def get_layer_margin(self, layer_number: int, balance: Optional[float] = None) -> float:
        """Absolute target margin (USDT) for a given 1-based layer.

        When `balance` is supplied, returns the balance-TIER margin
        (Layer 1 = tier['layer1'], Layer 2 = tier['layer2']). Layers beyond the
        configured pair reuse the last entry. When `balance` is None, falls back
        to the legacy fixed distribution (`basket_layer_margins_usd`).
        """
        if balance is not None:
            tier = self.get_basket_sizing_tier(balance)
            margins = [tier['layer1'], tier['layer2']]
        else:
            margins = self.basket_layer_margins_usd or [self.max_basket_margin_usd]
        idx = max(1, layer_number) - 1
        if idx >= len(margins):
            idx = len(margins) - 1
        return float(margins[idx])

    def get_target_margin_range(self, balance: float) -> tuple[float, float]:
        """(min, max) target margin for the FIRST entry layer, in USDT.

        Percentage of balance so sizing scales with account size:
        $20 → ~$0.50–$1.00. Clamped to the dust floor and the hard cap.
        """
        lo_pct, hi_pct = self.margin_target_pct_range
        hard_cap = self.get_margin_hard_cap(balance)
        lo = min(max(self.min_margin_floor, balance * lo_pct), hard_cap)
        hi = min(max(lo, balance * hi_pct), hard_cap)
        return (lo, hi)

    def get_daily_drawdown_limit(self, balance: float) -> float:
        """Account-size-aware daily drawdown limit (fraction).

        <= $50 → 0.15, $50–200 → 0.10, > $200 → 0.05.
        """
        for tier in self.daily_drawdown_tiers:
            if balance <= tier['max_balance']:
                return tier['drawdown_pct']
        return self.daily_drawdown_tiers[-1]['drawdown_pct']

    def is_preferred_symbol(self, symbol: str) -> bool:
        """True if the symbol's base asset is in the small-account preferred list."""
        base = symbol.split('/')[0].upper()
        return base in {
            s.split('/')[0].upper() for s in self.preferred_small_account_symbols
        }

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

        # NOTE: Master/VPS exchange API keys are intentionally NOT required.
        # The bot trades exclusively on per-user accounts loaded from the
        # database (decrypted with MASTER_ENCRYPTION_KEY). The only exchange
        # access the bot container itself needs is keyless PUBLIC market data.
        if self.recovery_max_layers < 1 or self.recovery_max_layers > 10:
            issues.append('recovery_max_layers must be between 1 and 10')
        if self.recovery_max_layers > 2:
            issues.append(
                'recovery_max_layers must not exceed 2 '
                '(Layer 1 initial entry + Layer 2 single recovery layer)'
            )
        if len(self.recovery_margin_multipliers) != self.recovery_max_layers:
            issues.append('recovery_margin_multipliers length must match recovery_max_layers')
        if len(self.recovery_atr_distances) != self.recovery_max_layers:
            issues.append('recovery_atr_distances length must match recovery_max_layers')
        if self.daily_loss_limit_pct <= 0 or self.daily_loss_limit_pct >= 1:
            issues.append('daily_loss_limit_pct must be between 0 and 1')

        # Legacy fixed distribution must still fit within the global ceiling.
        layer_sum = sum(self.basket_layer_margins_usd or [])
        if layer_sum > self.max_basket_margin_usd + 1e-9:
            issues.append(
                f'basket_layer_margins_usd sum ({layer_sum:.2f}) exceeds '
                f'max_basket_margin_usd ({self.max_basket_margin_usd:.2f})'
            )

        # Balance-tier basket sizing: each tier's two layers must fit its cap,
        # and no tier cap may exceed the absolute global ceiling.
        for tier in self.basket_sizing_tiers:
            layers_sum = tier.get('layer1', 0) + tier.get('layer2', 0)
            cap = tier.get('max_basket', 0)
            if layers_sum > cap + 1e-9:
                issues.append(
                    f"basket_sizing_tier layers ({layers_sum:.2f}) exceed its "
                    f"max_basket cap ({cap:.2f})"
                )
            if cap > self.max_basket_margin_usd + 1e-9:
                issues.append(
                    f"basket_sizing_tier cap ({cap:.2f}) exceeds global "
                    f"max_basket_margin_usd ({self.max_basket_margin_usd:.2f})"
                )

        # Daily profit trailing lock: each floor must be below its arming gain,
        # and every arming gain must be below the immediate hard-stop level.
        for t in self.daily_profit_lock_tiers:
            if t.get('floor', 0) >= t.get('gain', 0):
                issues.append('daily_profit_lock floor must be < its gain trigger')
            if t.get('gain', 0) >= self.daily_profit_hard_stop_pct:
                issues.append('daily_profit_lock gain must be < daily_profit_hard_stop_pct')

        # Loss-streak pause sanity.
        if self.loss_streak_threshold < 1:
            issues.append('loss_streak_threshold must be >= 1')
        if self.loss_streak_pause_seconds <= 0:
            issues.append('loss_streak_pause_seconds must be > 0')
        if self.profit_protection_floor_roi >= self.profit_protection_arm_roi:
            issues.append('profit_protection_floor_roi must be < profit_protection_arm_roi')
        if self.profit_protection_arm_roi >= self.basket_tp_target_roi:
            issues.append('profit_protection_arm_roi must be < basket_tp_target_roi')
        if self.max_exposure_pct <= 0 or self.max_exposure_pct >= 1:
            issues.append('max_exposure_pct must be between 0 and 1')
        if self.max_drawdown_pct <= 0 or self.max_drawdown_pct >= 1:
            issues.append('max_drawdown_pct must be between 0 and 1')

        return issues
