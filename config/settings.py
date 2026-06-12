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

    # ── Entry signal thresholds (configurable; control trade frequency) ──
    # LONG  enters when RSI < rsi_long_threshold (and the trend filter passes).
    # SHORT enters when RSI > rsi_short_threshold (and the trend filter passes).
    # Higher long / lower short = MORE frequent trades. Defaults 40/60 are a
    # commercial balance between frequency and avoiding random-noise entries.
    rsi_long_threshold: float = 40.0
    rsi_short_threshold: float = 60.0
    # When True (default) the EMA200 higher-timeframe trend filter is mandatory:
    # only long above EMA200, only short below. Set False to trade on RSI alone
    # (NOT recommended — removes trend protection for the averaging grid).
    require_ema_trend_filter: bool = True

    # ── Scanner ──
    max_watchlist_size: int = 25
    min_volume_24h: float = 50_000_000.0
    max_funding_rate: float = 0.001
    min_coin_age_days: int = 30
    # Composite-score boost applied to preferred small-account symbols so they
    # rank into the watchlist (0 = no boost). Keeps liquid, low-priced pairs in
    # rotation for $20–$100 accounts.
    preferred_symbol_score_boost: float = 0.15

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

    # ═════════════════════════════════════════
    # V2 — Market State Engine
    # ═════════════════════════════════════════
    btc_symbol: str = 'BTC/USDT'
    factor_timeframe: str = '4h'
    factor_ema_fast: int = 50
    factor_ema_slow: int = 200
    factor_adx_impulse_threshold: float = 25.0
    # A new BTC factor state must repeat on this many consecutive refreshes
    # before it replaces the current state (closed-candle hysteresis).
    factor_state_confirmations: int = 2
    market_state_refresh_seconds: int = 60
    vol_expansion_ratio: float = 1.4
    vol_compression_ratio: float = 0.75

    # ── V2 — Symbol State Engine ──
    # NEUTRAL band half-width = hysteresis_atr_mult × ATR(1h) around EMA200.
    hysteresis_atr_mult: float = 0.5
    # Minimum EMA slope (fraction over ~24 closed 1h bars) for STRONG states.
    strong_slope_pct: float = 0.01
    # Cached symbol states older than this are re-classified on demand.
    symbol_state_ttl_seconds: int = 900
    # Relative-strength lookback (closed 1h bars vs BTC).
    rs_lookback_bars: int = 24

    # ── V2 — Signal debouncing ──
    # A raw signal must be observed on this many consecutive evaluations
    # (within the window) before it is emitted — kills intra-candle phantom
    # signals that never exist on a closed chart.
    signal_confirmations: int = 2
    signal_confirmation_window_seconds: int = 90

    # ── V2 — Trade Templates ──
    # Every signal trades; the template decides size, ladder rights, and
    # patience. CORE = aligned with symbol + factor state; SCOUT =
    # counter-factor or demoted; RANGE = neutral-band / range-factor.
    template_policies: dict = field(default_factory=lambda: {
        'core': {
            'size_multiplier': 1.0, 'max_layers': 4,
            'risk_budget_pct': 0.012, 'tp_roi_multiplier': 1.0,
            'trailing_enabled': True, 'max_hold_hours': 72,
            'spacing_multiplier': 1.0,
        },
        'scout': {
            'size_multiplier': 0.5, 'max_layers': 2,
            'risk_budget_pct': 0.005, 'tp_roi_multiplier': 0.6,
            'trailing_enabled': False, 'max_hold_hours': 24,
            'spacing_multiplier': 1.5,
        },
        'range': {
            'size_multiplier': 0.6, 'max_layers': 2,
            'risk_budget_pct': 0.0075, 'tp_roi_multiplier': 0.6,
            'trailing_enabled': False, 'max_hold_hours': 36,
            'spacing_multiplier': 1.0,
        },
    })

    # ── V2 — Portfolio Manager ──
    # Total notional (margin × leverage) cap as a multiple of balance.
    max_total_notional_mult: float = 2.5
    # Notional cap for positions opposing the BTC factor direction.
    counter_factor_notional_cap_mult: float = 0.5
    # Max CORE-template baskets per correlation cluster per direction.
    max_core_per_cluster_direction: int = 2
    # Rolling event risk budget: realized losses + open basket risk budgets
    # within the window may not exceed this fraction of balance.
    event_risk_budget_pct: float = 0.06
    event_window_hours: int = 48
    # Correlation clusters by base asset (unlisted bases → 'alt' catch-all).
    # H4: the cluster cap (max_core_per_cluster_direction) is only as
    # granular as this map — with just BTC/ETH mapped, the whole alt market
    # collapsed into one cluster and the cap throttled the entire book.
    # Bases are matched as symbol.split('/')[0].upper(), so Binance
    # 1000-prefixed contracts appear as e.g. '1000PEPE'.
    symbol_clusters: dict = field(default_factory=lambda: {
        # Majors
        'BTC': 'major', 'ETH': 'major',
        # Layer-1 platforms
        'SOL': 'l1', 'BNB': 'l1', 'ADA': 'l1', 'AVAX': 'l1', 'DOT': 'l1',
        'ATOM': 'l1', 'NEAR': 'l1', 'APT': 'l1', 'SUI': 'l1', 'TON': 'l1',
        'TRX': 'l1', 'ALGO': 'l1', 'ICP': 'l1', 'SEI': 'l1', 'EGLD': 'l1',
        'HBAR': 'l1', 'VET': 'l1', 'FTM': 'l1', 'INJ': 'l1', 'TIA': 'l1',
        'KAS': 'l1',
        # Layer-2 / scaling
        'ARB': 'l2', 'OP': 'l2', 'MATIC': 'l2', 'POL': 'l2', 'STRK': 'l2',
        'IMX': 'l2', 'MNT': 'l2', 'METIS': 'l2', 'MANTA': 'l2', 'ZK': 'l2',
        # Memes
        'DOGE': 'meme', 'SHIB': 'meme', '1000SHIB': 'meme', 'PEPE': 'meme',
        '1000PEPE': 'meme', 'WIF': 'meme', 'BONK': 'meme', '1000BONK': 'meme',
        'FLOKI': 'meme', '1000FLOKI': 'meme', 'MEME': 'meme', 'BOME': 'meme',
        'NOT': 'meme', 'PENGU': 'meme', 'TRUMP': 'meme',
        # DeFi
        'UNI': 'defi', 'AAVE': 'defi', 'MKR': 'defi', 'CRV': 'defi',
        'COMP': 'defi', 'SNX': 'defi', 'SUSHI': 'defi', 'LDO': 'defi',
        'PENDLE': 'defi', 'DYDX': 'defi', 'JUP': 'defi', 'RUNE': 'defi',
        'CAKE': 'defi', 'GMX': 'defi',
        # Oracles / infrastructure / storage
        'LINK': 'infra', 'GRT': 'infra', 'FIL': 'infra', 'AR': 'infra',
        'THETA': 'infra', 'PYTH': 'infra', 'API3': 'infra', 'BAND': 'infra',
        # AI
        'FET': 'ai', 'RNDR': 'ai', 'RENDER': 'ai', 'AGIX': 'ai',
        'OCEAN': 'ai', 'TAO': 'ai', 'ARKM': 'ai', 'WLD': 'ai',
        'VIRTUAL': 'ai', 'AI16Z': 'ai',
        # Payments / legacy proof-of-work
        'XRP': 'payments', 'LTC': 'payments', 'BCH': 'payments',
        'XLM': 'payments', 'ETC': 'payments', 'ZEC': 'payments',
        'DASH': 'payments', 'EOS': 'payments', 'XMR': 'payments',
        # Gaming / metaverse
        'SAND': 'gaming', 'MANA': 'gaming', 'AXS': 'gaming',
        'GALA': 'gaming', 'APE': 'gaming', 'ENJ': 'gaming',
    })

    # ── V2 — Direction-aware post-loss response ──
    # After this many consecutive losses on one side (within the window),
    # that side trades SCOUT-only for the demotion duration or until a win.
    direction_demotion_losses: int = 3
    direction_demotion_window_hours: float = 12.0
    direction_demotion_duration_hours: float = 12.0

    # ── V2 — Exits ──
    # Trailing beyond the basket TP target: exit when ROI gives back this
    # fraction of the gain beyond the target (floor = the target itself).
    trailing_giveback_pct: float = 0.30
    # Break-even ratchet: arm once a ≥2-layer basket recovers to this ROI;
    # exit if ROI falls back to the floor (locks the recovery).
    be_ratchet_arm_roi: float = 0.02
    be_ratchet_floor_roi: float = 0.005
    # Wind-down (premise invalidated): exit at break-even-or-better, or at
    # market after the time budget expires.
    wind_down_max_hours: float = 12.0
    wind_down_be_epsilon_roi: float = 0.0
    # Time triage: close near-flat baskets older than the template's
    # max_hold_hours when |ROI| is inside this band (recycles the slot).
    time_triage_roi_band: float = 0.02

    # ── V2 — Account Profiles ──
    micro_account_max_balance: float = 75.0
    compact_account_max_balance: float = 250.0
    profile_policies: dict = field(default_factory=lambda: {
        'full': {'max_layers': 4, 'spacing_multiplier': 1.0},
        'compact': {'max_layers': 3, 'spacing_multiplier': 1.25},
        'micro': {'max_layers': 2, 'spacing_multiplier': 1.6},
    })

    # ── V2 — Watchlist tiers ──
    # Ranks 1..core → 'core', next secondary → 'secondary', next rotation →
    # 'rotation' (rotation symbols are capped below the CORE template).
    watchlist_tier_sizes: dict = field(default_factory=lambda: {
        'core': 20, 'secondary': 15, 'rotation': 15,
    })

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

    def get_margin_hard_cap(self, balance: float) -> float:
        """Hard ceiling on TOTAL margin per basket for this balance.

        $20→$2, $50→$5, $100→$10, $500→$50. Never below the dust floor.
        """
        return max(self.min_margin_floor, balance * self.margin_hard_cap_pct)

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

    # ── V2 helpers ──

    def get_template_policy(self, template: str) -> dict:
        """Trade-template management policy with safe defaults.

        Args:
            template: Template name ('core' | 'scout' | 'range').

        Returns:
            Policy dict with all keys present (missing keys default to the
            CORE/V1-equivalent values so legacy baskets behave unchanged).
        """
        defaults = {
            'size_multiplier': 1.0, 'max_layers': self.recovery_max_layers,
            'risk_budget_pct': 0.012, 'tp_roi_multiplier': 1.0,
            'trailing_enabled': False, 'max_hold_hours': 0,
            'spacing_multiplier': 1.0,
        }
        policy = self.template_policies.get((template or 'core').lower(), {})
        return {**defaults, **policy}

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

        # ── V2 validation ──
        for name in ('core', 'scout', 'range'):
            if name not in self.template_policies:
                issues.append(f'template_policies missing {name!r} template')
        if self.max_total_notional_mult <= 0:
            issues.append('max_total_notional_mult must be positive')
        if self.event_risk_budget_pct <= 0 or self.event_risk_budget_pct >= 1:
            issues.append('event_risk_budget_pct must be between 0 and 1')
        if self.hysteresis_atr_mult < 0:
            issues.append('hysteresis_atr_mult must be >= 0')
        if self.micro_account_max_balance >= self.compact_account_max_balance:
            issues.append(
                'micro_account_max_balance must be below compact_account_max_balance'
            )

        return issues
