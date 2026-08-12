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
from typing import Dict, List

from analysis.engine import TechnicalPicture
from analysis.structure import BEARISH, BULLISH, CHOCH, RANGE

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
}


def describe_modules() -> List[dict]:
    """Registry of every analysis module for the configuration UI.

    The single source of truth for which indicators exist, their weight, their
    category, and whether they are required. The frontend renders this list; it
    never invents indicators of its own.
    """
    return [
        {
            'key': key,
            'label': MODULE_LABEL.get(key, key),
            'category': MODULE_CATEGORY.get(key, 'Other'),
            'weight': MODULE_WEIGHTS[key],
            'required': key in REQUIRED_MODULES,
        }
        for key in MODULE_ORDER
    ]


def resolve_enabled_modules(requested) -> frozenset:
    """Resolve a user's requested module selection to the set actually used.

    Rules (see REQUIRED_MODULES for the rationale):
      * ``None`` → every module (the default, fully backward-compatible).
      * Otherwise → the REQUIRED modules are always forced on, plus whichever
        OPTIONAL modules were requested. Unknown/misspelled names are ignored
        (never trusted, never able to name arbitrary internals).

    Returns:
        A frozenset of valid module keys, always a superset of REQUIRED_MODULES.
    """
    if requested is None:
        return frozenset(MODULE_ORDER)
    requested_set = {str(m).strip().lower() for m in requested if str(m).strip()}
    chosen_optional = requested_set & OPTIONAL_MODULES
    return frozenset(REQUIRED_MODULES | chosen_optional)


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
    """Elliott wave count. Strength is the primary count's own confidence."""
    elliott = picture.elliott
    if not elliott.valid or elliott.primary is None:
        return ModuleVote(
            ELLIOTT, NEUTRAL_VOTE, 0.0, 'Undetermined', elliott.reason,
        )

    primary = elliott.primary
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
    """Participation behind the last candle.

    Volume has no direction of its own — it confirms whoever moved the candle.
    """
    ind = picture.indicators
    ratio = ind.volume_ratio
    if ind.volume_average <= 0:
        return ModuleVote(
            VOLUME, NEUTRAL_VOTE, 0.0, 'Unknown', 'no volume history available',
        )

    last = picture.candles.iloc[-1]
    candle_bullish = float(last['close']) >= float(last['open'])
    direction = BULLISH if candle_bullish else BEARISH

    if ratio >= 1.5:
        strength, label = 1.0, 'Strong'
    elif ratio >= 1.0:
        strength, label = 0.7, 'Above average'
    elif ratio >= 0.7:
        strength, label = 0.4, 'Average'
    else:
        # Thin participation confirms nothing at all.
        return ModuleVote(
            VOLUME, NEUTRAL_VOTE, 0.15, 'Weak',
            f'{ratio:.2f}x average volume — participation too thin to confirm',
        )

    return ModuleVote(
        VOLUME, direction, strength, label, f'{ratio:.2f}x average volume',
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
