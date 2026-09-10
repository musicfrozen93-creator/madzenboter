"""Module evaluation — the eight analysis components, each casting one vote.

Every signal must be justified by all eight of these, so each one is evaluated
on every request and reports:

    direction   which way it leans ('bullish' | 'bearish' | 'neutral')
    strength    how strongly, 0–1
    label       a short human-readable state ('Bullish BOS', 'Golden Pocket')
    detail      the numbers behind the label

The weights below are the Quality Score weighting, and they are also what the
Confluence Engine uses to aggregate votes — so "what makes a good setup" is
defined in exactly one place.

Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List

from analysis.structure import BEARISH, BULLISH, CHOCH, RANGE

if TYPE_CHECKING:
    # Type-only: `from __future__ import annotations` keeps every annotation a
    # string at runtime, so importing this lazily costs nothing and lets the ICT
    # confluence engine import the module-key constants defined below without a
    # circular import (modules → engine → ict → ict_confluence → modules).
    from analysis.engine import TechnicalPicture

BULLISH_VOTE = 'bullish'
BEARISH_VOTE = 'bearish'
NEUTRAL_VOTE = 'neutral'

# ── The eight classical modules (Phase 1) ──
TREND = 'trend'
STRUCTURE = 'structure'
ELLIOTT = 'elliott'
FIBONACCI = 'fibonacci'
VOLUME = 'volume'
RSI = 'rsi'
ATR = 'atr'
SUPPORT_RESISTANCE = 'support_resistance'

# ── The seven Smart Money confirmation modules (Phase 2) ──
ORDER_BLOCK = 'order_block'
FVG = 'fvg'
LIQUIDITY = 'liquidity'
VWAP = 'vwap'
MACD = 'macd'
ADX = 'adx'
PATTERN = 'pattern'

#: Maximum points each module contributes to the 100-point Quality Score.
#: These same numbers weight the confluence vote — "what makes a good setup" is
#: defined in exactly one place. Phase 2 rebalanced the eight classical modules
#: down to make room for the seven SMC confirmations while keeping the total 100.
MODULE_WEIGHTS: Dict[str, int] = {
    TREND: 15,
    STRUCTURE: 15,
    ELLIOTT: 10,
    FIBONACCI: 8,
    VOLUME: 8,
    RSI: 6,
    ATR: 4,
    SUPPORT_RESISTANCE: 8,
    ORDER_BLOCK: 8,
    FVG: 5,
    LIQUIDITY: 5,
    VWAP: 3,
    MACD: 3,
    ADX: 1,
    PATTERN: 1,
}

MODULE_ORDER: tuple[str, ...] = (
    TREND, STRUCTURE, ELLIOTT, FIBONACCI, VOLUME, RSI, ATR, SUPPORT_RESISTANCE,
    ORDER_BLOCK, FVG, LIQUIDITY, VWAP, MACD, ADX, PATTERN,
)

# ── ICT contextual modules (Phase 2A UI toggles) ──
# These are NOT weighted-vote modules — they never enter MODULE_WEIGHTS and never
# contribute a numerator/denominator to the Quality Score.  They are user-facing
# toggles that gate which ICT evidence is INCLUDED in the ICT-MSNR confluence
# panel and — via the ict_confluence toggle — whether that confluence feeds the
# confidence bonus at all.  Where the same underlying calculation already lives
# under the classical Smart Money modules (FVG, Order Blocks, Liquidity), the
# engine REUSES that calculation instead of computing it twice — see the
# evidence-registry deduplication in analysis.ict.ict_confluence.
ICT_LIQUIDITY = 'ict_liquidity'
ICT_LIQUIDITY_SWEEPS = 'ict_liquidity_sweeps'
ICT_MSS = 'ict_mss'
ICT_BOS = 'ict_bos'
ICT_DISPLACEMENT = 'ict_displacement'
ICT_FVG = 'ict_fvg'
ICT_ORDER_BLOCKS = 'ict_order_blocks'
ICT_PREMIUM_DISCOUNT = 'ict_premium_discount'
ICT_CONFLUENCE = 'ict_confluence'

ICT_MODULES: tuple[str, ...] = (
    ICT_LIQUIDITY, ICT_LIQUIDITY_SWEEPS, ICT_MSS, ICT_BOS,
    ICT_DISPLACEMENT, ICT_FVG, ICT_ORDER_BLOCKS, ICT_PREMIUM_DISCOUNT,
    ICT_CONFLUENCE,
)

# ── MSNR contextual modules (Phase 2A UI toggles) ──
# Same contract as ICT_MODULES: contextual only, weight zero, exposed in the
# UI so the user can turn any component off.  Support/resistance zones share
# the underlying levels engine with SUPPORT_RESISTANCE — the shared evidence
# is deduplicated so a level is never counted twice across the two views.
MSNR_SUPPORT_ZONES = 'msnr_support_zones'
MSNR_RESISTANCE_ZONES = 'msnr_resistance_zones'
MSNR_PDHL = 'msnr_pdhl'
MSNR_PWHL = 'msnr_pwhl'
MSNR_SWING_LEVELS = 'msnr_swing_levels'
MSNR_KEY_SR = 'msnr_key_sr'
MSNR_LOCATION = 'msnr_location'

MSNR_MODULES: tuple[str, ...] = (
    MSNR_SUPPORT_ZONES, MSNR_RESISTANCE_ZONES, MSNR_PDHL, MSNR_PWHL,
    MSNR_SWING_LEVELS, MSNR_KEY_SR, MSNR_LOCATION,
)

CONTEXTUAL_MODULES: tuple[str, ...] = ICT_MODULES + MSNR_MODULES

# Every selectable module (weighted vote + contextual), in the exact order the
# UI displays them.  This is what the user sees and what resolve_enabled_modules
# validates against.
ALL_MODULE_KEYS: tuple[str, ...] = MODULE_ORDER + CONTEXTUAL_MODULES

# Modules that measure market CONTEXT/STRENGTH rather than direction. In the
# Quality Score they always earn their weight scaled by suitability, never
# zeroed for "opposing", because they have no side to oppose with.
STRENGTH_MODULES: frozenset = frozenset({ATR, ADX})

# ── Required vs optional (user-configurable) modules ──
# The user may switch OPTIONAL modules off; REQUIRED modules are fundamental to
# reading direction/structure and can never be disabled — the engine would have
# nothing to anchor a signal to without them.
#
# A disabled optional module is EXCLUDED from the analysis entirely: it casts no
# vote, so it counts neither FOR nor AGAINST the setup, and its weight leaves
# BOTH the numerator and the denominator of the Quality/Confidence scores. It is
# never treated as bearish/bullish evidence. See analysis.scoring for the exact
# normalisation.
REQUIRED_MODULES: frozenset = frozenset({TREND, STRUCTURE})
OPTIONAL_MODULES: frozenset = frozenset(MODULE_ORDER) - REQUIRED_MODULES
# Optional keys the UI may toggle. Includes the contextual ICT/MSNR keys so the
# request-side resolver accepts them, even though they carry weight zero and
# never enter MODULE_WEIGHTS.
OPTIONAL_UI_KEYS: frozenset = OPTIONAL_MODULES | frozenset(CONTEXTUAL_MODULES)

# Human-facing registry, surfaced to the dashboard so the UI never hardcodes a
# second indicator list. category groups the checkboxes; required==True renders
# as an always-on "Core Analysis" item.
MODULE_CATEGORY: Dict[str, str] = {
    TREND: 'Trend & Structure',
    STRUCTURE: 'Trend & Structure',
    ELLIOTT: 'Price Action',
    FIBONACCI: 'Price Action',
    SUPPORT_RESISTANCE: 'Price Action',
    PATTERN: 'Price Action',
    RSI: 'Momentum',
    MACD: 'Momentum',
    ADX: 'Momentum',
    VOLUME: 'Volume & Volatility',
    ATR: 'Volume & Volatility',
    VWAP: 'Volume & Volatility',
    ORDER_BLOCK: 'Smart Money',
    FVG: 'Smart Money',
    LIQUIDITY: 'Smart Money',
    # ICT
    ICT_LIQUIDITY: 'ICT',
    ICT_LIQUIDITY_SWEEPS: 'ICT',
    ICT_MSS: 'ICT',
    ICT_BOS: 'ICT',
    ICT_DISPLACEMENT: 'ICT',
    ICT_FVG: 'ICT',
    ICT_ORDER_BLOCKS: 'ICT',
    ICT_PREMIUM_DISCOUNT: 'ICT',
    ICT_CONFLUENCE: 'ICT',
    # MSNR
    MSNR_SUPPORT_ZONES: 'MSNR',
    MSNR_RESISTANCE_ZONES: 'MSNR',
    MSNR_PDHL: 'MSNR',
    MSNR_PWHL: 'MSNR',
    MSNR_SWING_LEVELS: 'MSNR',
    MSNR_KEY_SR: 'MSNR',
    MSNR_LOCATION: 'MSNR',
}

MODULE_LABEL: Dict[str, str] = {
    TREND: 'Trend (multi-timeframe)',
    STRUCTURE: 'Market Structure',
    ELLIOTT: 'Elliott Wave',
    FIBONACCI: 'Fibonacci',
    VOLUME: 'Volume',
    RSI: 'RSI',
    ATR: 'ATR / Volatility',
    SUPPORT_RESISTANCE: 'Support & Resistance',
    ORDER_BLOCK: 'Order Blocks',
    FVG: 'Fair Value Gaps',
    LIQUIDITY: 'Liquidity',
    VWAP: 'VWAP',
    MACD: 'MACD',
    ADX: 'ADX (Trend Strength)',
    PATTERN: 'Chart Patterns',
    # ICT
    ICT_LIQUIDITY: 'Liquidity Analysis',
    ICT_LIQUIDITY_SWEEPS: 'Liquidity Sweeps',
    ICT_MSS: 'Market Structure Shift (MSS)',
    ICT_BOS: 'Break of Structure (BOS)',
    ICT_DISPLACEMENT: 'Displacement',
    ICT_FVG: 'Fair Value Gaps (FVG)',
    ICT_ORDER_BLOCKS: 'Order Blocks',
    ICT_PREMIUM_DISCOUNT: 'Premium / Discount',
    ICT_CONFLUENCE: 'ICT Confluence',
    # MSNR
    MSNR_SUPPORT_ZONES: 'Support Zones',
    MSNR_RESISTANCE_ZONES: 'Resistance Zones',
    MSNR_PDHL: 'Previous Day High / Low',
    MSNR_PWHL: 'Previous Week High / Low',
    MSNR_SWING_LEVELS: 'Swing Levels',
    MSNR_KEY_SR: 'Key S/R Zones',
    MSNR_LOCATION: 'MSNR Location Analysis',
}


def describe_modules() -> List[dict]:
    """Registry of every analysis module for the configuration UI.

    The single source of truth for which indicators exist, their weight, their
    category, and whether they are required. The frontend renders this list; it
    never invents indicators of its own.

    Includes both weighted-vote modules (MODULE_ORDER) and the contextual
    ICT/MSNR toggles (CONTEXTUAL_MODULES).  Contextual toggles have weight zero
    — they do not contribute to the Quality Score denominator; disabling them
    just excludes their evidence from the ICT / MSNR panels and the confluence
    bonus, never as points for or against the setup.
    """
    return [
        {
            'key': key,
            'label': MODULE_LABEL.get(key, key),
            'category': MODULE_CATEGORY.get(key, 'Other'),
            'weight': MODULE_WEIGHTS.get(key, 0),
            'required': key in REQUIRED_MODULES,
        }
        for key in ALL_MODULE_KEYS
    ]


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY PRESETS
# ─────────────────────────────────────────────────────────────────────────────
# A preset is a named, explicit set of OPTIONAL module keys. It is the single
# source of truth for "what does ICT + MSNR mean" — the dashboard renders these
# sets, it never invents its own.
#
# WHY THE SMART MONEY MODULES ARE PART OF THE ICT + MSNR PRESET
# -------------------------------------------------------------
# The ICT_* and MSNR_* keys are CONTEXTUAL toggles: weight zero, no vote, no
# quality points. They decide which ICT/MSNR evidence is shown and whether the
# confluence bonus applies. They cannot, on their own, score anything.
#
# The evidence they describe is produced by the weighted Smart Money modules:
# ICTMSNRConfluenceEngine.evaluate() is handed the very same LiquidityState,
# FairValueGapState and OrderBlockState objects that ORDER_BLOCK, FVG and
# LIQUIDITY vote on, and MSNR's zones come from the same levels engine as
# SUPPORT_RESISTANCE. There is exactly ONE underlying calculation per event; the
# EvidenceRegistry in analysis.ict.evidence deduplicates by evidence_id so a
# swing/level/gap seen by several layers is stored once.
#
# So enabling the ICT/MSNR toggles WITHOUT their weighted carriers would leave
# the primary strategy with no scoring weight at all — the preset would be
# cosmetic. ICT + MSNR therefore enables ORDER_BLOCK, FVG, LIQUIDITY and
# SUPPORT_RESISTANCE as the canonical, deduplicated carriers of that evidence.

#: Confirmation layer — context and participation, never the primary read.
CONFIRMATION_MODULES: tuple[str, ...] = (VOLUME, ATR, VWAP)

#: The weighted modules that carry ICT/MSNR evidence into the score. Enabling
#: an ICT/MSNR toggle without these would produce evidence nothing can score.
ICT_MSNR_CARRIERS: tuple[str, ...] = (
    ORDER_BLOCK, FVG, LIQUIDITY, SUPPORT_RESISTANCE,
)

PRESET_ICT_MSNR = 'ict_msnr'
PRESET_BALANCED = 'balanced'
PRESET_CONSERVATIVE = 'conservative'
PRESET_CUSTOM = 'custom'

#: Yaksha AI's primary trading methodology, and the default for NEW analyses.
_ICT_MSNR_KEYS: frozenset = frozenset(
    ICT_MODULES + MSNR_MODULES + CONFIRMATION_MODULES + ICT_MSNR_CARRIERS
)

#: Classical technical analysis: structure, levels and momentum, no ICT/MSNR
#: overlay. Excludes the probabilistic wave count and the two 1-point modules
#: whose signal is too thin to change a decision.
_CONSERVATIVE_KEYS: frozenset = frozenset(
    (SUPPORT_RESISTANCE, FIBONACCI, RSI, ORDER_BLOCK, FVG, LIQUIDITY)
    + CONFIRMATION_MODULES
)

#: Everything the engine offers — the historical default.
_BALANCED_KEYS: frozenset = frozenset(OPTIONAL_UI_KEYS)

PRESETS: Dict[str, frozenset] = {
    PRESET_ICT_MSNR: _ICT_MSNR_KEYS,
    PRESET_BALANCED: _BALANCED_KEYS,
    PRESET_CONSERVATIVE: _CONSERVATIVE_KEYS,
}

PRESET_LABEL: Dict[str, str] = {
    PRESET_ICT_MSNR: 'ICT + MSNR',
    PRESET_BALANCED: 'Balanced',
    PRESET_CONSERVATIVE: 'Conservative',
}

PRESET_DESCRIPTION: Dict[str, str] = {
    PRESET_ICT_MSNR: (
        'Smart-money structure and reaction levels, scored through their '
        'deduplicated evidence carriers and confirmed by volume, volatility '
        'and VWAP.'
    ),
    PRESET_BALANCED: 'Every module the engine offers.',
    PRESET_CONSERVATIVE: (
        'Classical structure, levels and momentum. No ICT or MSNR overlay, no '
        'probabilistic wave count.'
    ),
}

#: What a NEW analysis uses when the user has expressed no preference.
DEFAULT_PRESET: str = PRESET_ICT_MSNR

# Every preset must name only real optional keys.
for _pid, _keys in PRESETS.items():
    _unknown = _keys - OPTIONAL_UI_KEYS
    assert not _unknown, f'preset {_pid} names unknown modules: {sorted(_unknown)}'

# The ICT + MSNR preset must leave exactly the six modules the methodology
# deliberately excludes switched off.
assert (frozenset(MODULE_ORDER) - REQUIRED_MODULES) - _ICT_MSNR_KEYS == frozenset(
    {ELLIOTT, FIBONACCI, RSI, MACD, ADX, PATTERN}
), 'ICT + MSNR must disable exactly Elliott, Fibonacci, RSI, MACD, ADX and Patterns'


class UnknownPresetError(ValueError):
    """Raised when a caller names a preset the registry does not define."""


def preset_modules(preset_id: str) -> frozenset:
    """Full module set for a preset — required core plus its optional keys.

    An unknown id RAISES. It used to fall back to the default preset, which is
    the production methodology — so any unrecognised string (a typo, a stale
    client, or the literal ``'custom'`` this API echoes back in its own
    responses) silently resolved to a tradeable production configuration. A
    name the registry does not define is an error, not a licence to trade.
    """
    keys = PRESETS.get(str(preset_id or '').strip().lower())
    if keys is None:
        raise UnknownPresetError(
            f'Unknown preset {preset_id!r}. Choose one of: '
            f'{", ".join(sorted(PRESETS))}.'
        )
    return frozenset(REQUIRED_MODULES | keys)


def identify_preset(active) -> str:
    """Name the preset an enabled-module set corresponds to.

    Returns the preset id when the OPTIONAL portion matches one exactly,
    otherwise ``'custom'``. Used by the serializer so a response states which
    methodology produced it.
    """
    optional = frozenset(active) & OPTIONAL_UI_KEYS
    for pid in (PRESET_ICT_MSNR, PRESET_CONSERVATIVE, PRESET_BALANCED):
        if optional == PRESETS[pid]:
            return pid
    return PRESET_CUSTOM


def describe_presets() -> List[dict]:
    """Preset registry for the configuration UI.

    Mirrors ``describe_modules``: the dashboard renders this and never hardcodes
    a second definition of what a strategy preset contains.
    """
    return [
        {
            'id': pid,
            'label': PRESET_LABEL[pid],
            'description': PRESET_DESCRIPTION[pid],
            'modules': sorted(PRESETS[pid]),
            'default': pid == DEFAULT_PRESET,
        }
        for pid in (PRESET_ICT_MSNR, PRESET_BALANCED, PRESET_CONSERVATIVE)
    ]


def resolve_enabled_modules(requested) -> frozenset:
    """Resolve a user's requested module selection to the set actually used.

    Rules (see REQUIRED_MODULES for the rationale):
      * ``None`` → every module (the default, fully backward-compatible).
      * Otherwise → the REQUIRED modules are always forced on, plus whichever
        OPTIONAL modules were requested (including any ICT/MSNR contextual
        toggles). Unknown/misspelled names are ignored (never trusted, never
        able to name arbitrary internals).

    Returns:
        A frozenset of valid module keys, always a superset of REQUIRED_MODULES.
        May contain both weighted MODULE_ORDER keys and contextual
        CONTEXTUAL_MODULES keys.
    """
    if requested is None:
        return frozenset(ALL_MODULE_KEYS)
    requested_set = {str(m).strip().lower() for m in requested if str(m).strip()}
    chosen_optional = requested_set & OPTIONAL_UI_KEYS
    return frozenset(REQUIRED_MODULES | chosen_optional)


def ict_enabled(active: frozenset) -> bool:
    """True when at least one ICT toggle is on.

    Used by the analysis engine to skip ICT evidence computation and by the
    serializer to omit the ICT panel from the response so a disabled category
    never appears as active evidence.
    """
    return bool(active & frozenset(ICT_MODULES))


def msnr_enabled(active: frozenset) -> bool:
    """True when at least one MSNR toggle is on.

    Mirrors ``ict_enabled`` for MSNR — when every MSNR toggle is off the engine
    skips the MSNR calculation entirely and no MSNR data appears in the
    response.
    """
    return bool(active & frozenset(MSNR_MODULES))


assert sum(MODULE_WEIGHTS.values()) == 100, 'module weights must total 100'


@dataclass(frozen=True)
class ModuleVote:
    """One module's read of the market."""

    module: str
    direction: str          # 'bullish' | 'bearish' | 'neutral'
    strength: float         # 0–1
    label: str              # short state, shown in the breakdown
    detail: str             # the numbers behind it

    @property
    def weight(self) -> int:
        return MODULE_WEIGHTS[self.module]

    @property
    def weighted_strength(self) -> float:
        """Strength scaled by the module's weight — its vote size."""
        return self.strength * self.weight

    def agrees_with(self, direction: str) -> bool:
        """True when this module leans the same way as a trade direction."""
        wanted = BULLISH_VOTE if direction == 'long' else BEARISH_VOTE
        return self.direction == wanted

    def opposes(self, direction: str) -> bool:
        """True when this module leans against a trade direction."""
        unwanted = BEARISH_VOTE if direction == 'long' else BULLISH_VOTE
        return self.direction == unwanted


def evaluate_modules(mtf) -> List[ModuleVote]:
    """Evaluate all fifteen modules for a multi-timeframe picture.

    Args:
        mtf: An `analysis.timeframes.MultiTimeframePicture`.

    Returns:
        One vote per module, in `MODULE_ORDER`. No module can create a signal on
        its own — these votes are aggregated by the Confluence Engine.
    """
    entry = mtf.entry
    return [
        # Classical
        _trend_vote(mtf),
        _structure_vote(entry),
        _elliott_vote(entry),
        _fibonacci_vote(entry),
        _volume_vote(entry),
        _rsi_vote(entry),
        _atr_vote(entry),
        _support_resistance_vote(entry),
        # Smart Money confirmations
        _order_block_vote(entry),
        _fvg_vote(entry),
        _liquidity_vote(entry),
        _vwap_vote(entry),
        _macd_vote(entry),
        _adx_vote(entry),
        _pattern_vote(entry),
    ]


# ─────────────────────────────────────────────
# Individual modules
# ─────────────────────────────────────────────

def _trend_vote(mtf) -> ModuleVote:
    """Trend across the internal timeframe ladder.

    Strength is the share of ladder weight agreeing, so a setup confirmed on all
    three timeframes scores full marks and one confirmed only on the entry
    timeframe scores its rung weight alone.
    """
    direction = mtf.aligned_direction
    alignment = mtf.alignment

    if direction == RANGE:
        return ModuleVote(
            TREND, NEUTRAL_VOTE, 0.0, 'Range',
            'no consistent direction across the internal timeframes',
        )

    htf = mtf.higher_timeframe_direction
    label = 'Bullish' if direction == BULLISH else 'Bearish'
    if htf == direction and mtf.ladder.is_multi:
        label = f'{label} (HTF confirmed)'

    return ModuleVote(
        TREND, direction, alignment, label,
        f'{alignment:.0%} of the internal timeframe weight is {direction}',
    )


def _structure_vote(picture: TechnicalPicture) -> ModuleVote:
    """Market structure: HH/HL sequencing plus the latest BOS or CHoCH."""
    structure = picture.structure

    if not structure.has_structure:
        return ModuleVote(
            STRUCTURE, NEUTRAL_VOTE, 0.0, 'Undefined',
            'not enough confirmed swings to read structure',
        )

    if structure.trend == RANGE:
        return ModuleVote(
            STRUCTURE, NEUTRAL_VOTE, 0.0, 'Range', structure.reason,
        )

    strength = structure.strength
    label = 'Bullish' if structure.trend == BULLISH else 'Bearish'

    if structure.event and structure.event_direction == structure.trend:
        # A break continuing the trend is the strongest structural confirmation.
        label = f'{label} {structure.event.upper()}'
        strength = min(1.0, strength + 0.25 * min(1.0, structure.event_strength))
    elif structure.event == CHOCH:
        # A change of character warns the current structure may be ending.
        label = f'{label} (CHoCH warning)'
        strength = max(0.0, strength - 0.4)

    return ModuleVote(
        STRUCTURE, structure.trend, round(strength, 4), label, structure.reason,
    )


def _elliott_vote(picture: TechnicalPicture) -> ModuleVote:
    """Elliott wave count.  Strength is the primary count's own confidence.

    Phase 3B: probabilistic-uncertainty guard.  If the primary and the
    alternative differ by less than ``MIN_PROBABILITY_EDGE`` percentage
    points the read is effectively a coin flip (a 51/49 count is NOT
    directional evidence), so the module votes NEUTRAL with zero strength
    and its confidence component drops to zero.  The threshold is exposed
    via ``analysis.scoring.MIN_PROBABILITY_EDGE`` and applies to every
    probabilistic ranking, not just Elliott.
    """
    # Local import — the modules → scoring → modules cycle is broken by
    # keeping this at call time instead of module load time.
    from analysis.scoring import MIN_PROBABILITY_EDGE

    elliott = picture.elliott
    if not elliott.valid or elliott.primary is None:
        return ModuleVote(
            ELLIOTT, NEUTRAL_VOTE, 0.0, 'Undetermined', elliott.reason,
        )

    primary = elliott.primary
    alt = elliott.alternative
    if alt is not None:
        edge = primary.confidence - alt.confidence
        if edge < MIN_PROBABILITY_EDGE:
            return ModuleVote(
                ELLIOTT, NEUTRAL_VOTE, 0.0,
                f'Uncertain ({primary.label} vs {alt.label})',
                f'{primary.label} {primary.confidence:.0f}% vs '
                f'{alt.label} {alt.confidence:.0f}% — edge of {edge:.0f} pp '
                f'is below the {MIN_PROBABILITY_EDGE:.0f} pp uncertainty '
                f'threshold, treated as neutral',
            )

    strength = primary.confidence / 100.0
    if primary.rules_violated:
        # A count that breaks a hard rule still informs, but weakly.
        strength *= 0.5

    return ModuleVote(
        ELLIOTT, primary.direction, round(strength, 4), primary.label,
        f'{primary.label} ({primary.confidence:.0f}% confidence, '
        f'{primary.completion_pct:.0f}% complete)',
    )


def _fibonacci_vote(picture: TechnicalPicture) -> ModuleVote:
    """Fibonacci position within the dominant leg."""
    fib = picture.fibonacci
    if not fib.valid:
        return ModuleVote(
            FIBONACCI, NEUTRAL_VOTE, 0.0, 'No leg', fib.reason,
        )

    direction = BULLISH if fib.direction == 'up' else BEARISH
    ratio = fib.current_ratio

    # A leg retraced beyond 100% is broken — a failed leg is evidence FOR the
    # other side, so the vote flips.
    if ratio > 1.0:
        opposite = BEARISH if direction == BULLISH else BULLISH
        return ModuleVote(
            FIBONACCI, opposite, 0.4, 'Leg invalidated',
            f'price retraced {ratio:.0%} of the {fib.direction} leg, breaking it',
        )

    # A negative ratio means price extended past the leg's own extreme: the move
    # is running, which supports its direction, but the entry is late.
    if ratio < 0:
        return ModuleVote(
            FIBONACCI, direction, 0.35, f'Extended {-ratio:.0%} beyond the leg',
            fib.reason,
        )

    if fib.in_golden_pocket:
        strength, label = 1.0, 'Golden Pocket'
    elif 0.382 <= ratio <= 0.786:
        strength, label = 0.7, f'{ratio:.1%} retracement'
    elif ratio < 0.382:
        # Shallow: trend is strong but the entry is late.
        strength, label = 0.45, f'Shallow {ratio:.1%} retracement'
    else:
        strength, label = 0.3, f'Deep {ratio:.1%} retracement'

    return ModuleVote(FIBONACCI, direction, strength, label, fib.reason)


def _volume_vote(picture: TechnicalPicture) -> ModuleVote:
    """Volume-weighted directional read over recent candles.

    A single candle's colour is noisy; instead we weight the last N candles by
    their volume share and measure the net directional pressure. The result is
    BULLISH, BEARISH, or NEUTRAL — never a strong directional call on thin or
    ambiguous evidence.
    """
    LOOKBACK = 5

    ind = picture.indicators
    ratio = ind.volume_ratio
    if ind.volume_average <= 0:
        return ModuleVote(
            VOLUME, NEUTRAL_VOTE, 0.0, 'Unknown', 'no volume history available',
        )

    candles = picture.candles
    if len(candles) < LOOKBACK:
        return ModuleVote(
            VOLUME, NEUTRAL_VOTE, 0.1, 'Insufficient',
            f'only {len(candles)} candles, need {LOOKBACK} for volume analysis',
        )

    recent = candles.iloc[-LOOKBACK:]
    total_vol = float(recent['volume'].sum())
    if total_vol <= 0:
        return ModuleVote(
            VOLUME, NEUTRAL_VOTE, 0.0, 'Unknown', 'zero volume in recent candles',
        )

    bull_vol = 0.0
    bear_vol = 0.0
    for _, c in recent.iterrows():
        v = float(c['volume'])
        if float(c['close']) >= float(c['open']):
            bull_vol += v
        else:
            bear_vol += v

    bull_share = bull_vol / total_vol
    bear_share = bear_vol / total_vol
    imbalance = abs(bull_share - bear_share)

    if imbalance < 0.15:
        direction = NEUTRAL_VOTE
    elif bull_share > bear_share:
        direction = BULLISH
    else:
        direction = BEARISH

    if ratio < 0.7:
        return ModuleVote(
            VOLUME, NEUTRAL_VOTE, 0.15, 'Weak',
            f'{ratio:.2f}x avg vol — participation too thin to confirm',
        )

    if direction == NEUTRAL_VOTE:
        return ModuleVote(
            VOLUME, NEUTRAL_VOTE, 0.3, 'Mixed',
            f'{ratio:.2f}x avg vol, {bull_share:.0%} buy / {bear_share:.0%} sell — no clear side',
        )

    if ratio >= 1.5:
        strength, label = 1.0, 'Strong'
    elif ratio >= 1.0:
        strength, label = 0.7, 'Above average'
    else:
        strength, label = 0.4, 'Average'

    return ModuleVote(
        VOLUME, direction, strength, label,
        f'{ratio:.2f}x avg vol, {bull_share:.0%} buy / {bear_share:.0%} sell',
    )


def _rsi_vote(picture: TechnicalPicture) -> ModuleVote:
    """Momentum. Extremes are treated as exhaustion, not confirmation."""
    rsi = picture.indicators.rsi
    label = picture.indicators.rsi_label
    detail = f'RSI {rsi:.1f} ({label})'

    if rsi >= 70:
        # Overbought: momentum is bullish but stretched — weak confirmation.
        return ModuleVote(RSI, BULLISH_VOTE, 0.3, 'Overbought', detail)
    if rsi <= 30:
        return ModuleVote(RSI, BEARISH_VOTE, 0.3, 'Oversold', detail)
    if rsi >= 55:
        return ModuleVote(RSI, BULLISH_VOTE, 0.9, 'Bullish', detail)
    if rsi <= 45:
        return ModuleVote(RSI, BEARISH_VOTE, 0.9, 'Bearish', detail)
    return ModuleVote(RSI, NEUTRAL_VOTE, 0.2, 'Neutral', detail)


def _atr_vote(picture: TechnicalPicture) -> ModuleVote:
    """Volatility suitability.

    ATR never has a direction. It reports whether volatility is workable: too
    compressed and there is no move to capture, too elevated and stops must be
    so wide the trade stops making sense.
    """
    ind = picture.indicators
    label = ind.volatility_label
    detail = (
        f'ATR {ind.atr_pct:.2%} of price vs {ind.atr_average_pct:.2%} average'
    )

    if label == 'normal':
        strength = 1.0
    elif label == 'elevated':
        strength = 0.4
    elif label == 'compressed':
        strength = 0.5
    else:
        strength = 0.0

    return ModuleVote(ATR, NEUTRAL_VOTE, strength, label.title(), detail)


def _support_resistance_vote(picture: TechnicalPicture) -> ModuleVote:
    """Position relative to the nearest support and resistance zones."""
    levels = picture.levels
    ind = picture.indicators

    if not levels.has_levels:
        return ModuleVote(
            SUPPORT_RESISTANCE, NEUTRAL_VOTE, 0.0, 'None', levels.reason,
        )

    position = levels.position_label(ind.atr)
    support = levels.nearest_support
    resistance = levels.nearest_resistance

    if position in ('at support', 'just above support') and support:
        return ModuleVote(
            SUPPORT_RESISTANCE, BULLISH_VOTE, support.strength,
            'Strong Support' if support.strength >= 0.6 else 'Support',
            f'{position} at {support.center:.8f} ({support.touches} touches)',
        )
    if position in ('at resistance', 'just below resistance') and resistance:
        return ModuleVote(
            SUPPORT_RESISTANCE, BEARISH_VOTE, resistance.strength,
            'Strong Resistance' if resistance.strength >= 0.6 else 'Resistance',
            f'{position} at {resistance.center:.8f} ({resistance.touches} touches)',
        )

    # Mid-range: whichever side has more room is mildly favoured, because a
    # trade needs somewhere to travel before it meets an obstacle.
    up = levels.room_to_resistance()
    down = levels.room_to_support()
    if up is not None and down is not None and ind.atr > 0:
        if up > down * 1.5:
            return ModuleVote(
                SUPPORT_RESISTANCE, BULLISH_VOTE, 0.4, 'Room above',
                f'{up / ind.atr:.1f} ATR to resistance vs {down / ind.atr:.1f} ATR to support',
            )
        if down > up * 1.5:
            return ModuleVote(
                SUPPORT_RESISTANCE, BEARISH_VOTE, 0.4, 'Room below',
                f'{down / ind.atr:.1f} ATR to support vs {up / ind.atr:.1f} ATR to resistance',
            )

    return ModuleVote(
        SUPPORT_RESISTANCE, NEUTRAL_VOTE, 0.2, 'Mid-range',
        f'{position}, no dominant level nearby',
    )


# ─────────────────────────────────────────────
# Smart Money confirmation modules (Phase 2)
# ─────────────────────────────────────────────

def _order_block_vote(picture: TechnicalPicture) -> ModuleVote:
    """Nearest fresh/mitigated order block relative to price."""
    ob = picture.order_blocks
    if ob.nearest is None or ob.direction == 'neutral':
        return ModuleVote(ORDER_BLOCK, NEUTRAL_VOTE, 0.0, 'None', ob.reason)
    label = f'{ob.nearest.state.title()} {ob.direction.title()} OB'
    return ModuleVote(ORDER_BLOCK, ob.direction, ob.score, label, ob.reason)


def _fvg_vote(picture: TechnicalPicture) -> ModuleVote:
    """Nearest unfilled fair value gap."""
    fvg = picture.fair_value_gaps
    if fvg.nearest is None or fvg.direction == 'neutral':
        return ModuleVote(FVG, NEUTRAL_VOTE, 0.0, 'None', fvg.reason)
    label = f'{fvg.direction.title()} FVG'
    return ModuleVote(FVG, fvg.direction, fvg.score, label, fvg.reason)


def _liquidity_vote(picture: TechnicalPicture) -> ModuleVote:
    """Most recent liquidity sweep — contrarian to the swept side."""
    liq = picture.liquidity
    if liq.last_sweep is None or liq.direction == 'neutral':
        equal = liq.equal_highs + liq.equal_lows
        label = f'{equal} pools' if equal else 'None'
        return ModuleVote(LIQUIDITY, NEUTRAL_VOTE, 0.0, label, liq.reason)
    label = 'Liquidity Grab' if liq.last_sweep.grabbed else 'Liquidity Sweep'
    return ModuleVote(LIQUIDITY, liq.direction, liq.score, label, liq.reason)


def _vwap_vote(picture: TechnicalPicture) -> ModuleVote:
    """Price relative to the anchored VWAP."""
    vwap = picture.vwap
    if vwap.direction == 'neutral':
        return ModuleVote(VWAP, NEUTRAL_VOTE, vwap.score, 'At VWAP', vwap.reason)
    label = 'Above VWAP' if vwap.above else 'Below VWAP'
    return ModuleVote(VWAP, vwap.direction, vwap.score, label, vwap.reason)


def _macd_vote(picture: TechnicalPicture) -> ModuleVote:
    """MACD momentum and crosses."""
    macd = picture.macd
    if macd.direction == 'neutral':
        return ModuleVote(MACD, NEUTRAL_VOTE, macd.score, 'Flat', macd.reason)
    if macd.bullish_cross:
        label = 'Bullish Cross'
    elif macd.bearish_cross:
        label = 'Bearish Cross'
    else:
        label = f'{macd.momentum.title()} Momentum'
    return ModuleVote(MACD, macd.direction, macd.score, label, macd.reason)


def _adx_vote(picture: TechnicalPicture) -> ModuleVote:
    """Trend strength (a STRENGTH module — it confirms, it does not oppose).

    ADX measures whether a trend is worth trading. It reports the DI direction
    for the breakdown, but votes NEUTRAL so a strong trend never counts against
    the very direction the other modules found — it only adds conviction.
    """
    adx = picture.adx
    label = {
        'strong': 'Strong Trend', 'weak': 'Weak Trend', 'no_trend': 'No Trend',
    }.get(adx.trend_strength, 'No Trend')
    return ModuleVote(ADX, NEUTRAL_VOTE, adx.score, label, adx.reason)


def _pattern_vote(picture: TechnicalPicture) -> ModuleVote:
    """Recognised chart pattern."""
    pat = picture.patterns
    if pat.pattern is None or pat.direction == 'neutral':
        label = pat.name if pat.pattern else 'None'
        return ModuleVote(PATTERN, NEUTRAL_VOTE, pat.score, label, pat.reason)
    return ModuleVote(PATTERN, pat.direction, pat.score, pat.name, pat.reason)
