"""Signal Explanation Engine — beginner and professional narratives.

Turns the analysis into three human-readable forms:

  • summary        a short professional paragraph explaining the signal
  • professional   terse, terminology-rich bullet points
  • beginner       the same story in plain language, no jargon

This is a deterministic template layer. It is NOT AI: the same analysis always
produces the same words, and it only restates what the Analysis Engine found. It
never invents a confirmation the modules did not report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from analysis.intelligence.context import SignalContext
from analysis.modules import (
    ELLIOTT,
    FIBONACCI,
    FVG,
    LIQUIDITY,
    MACD,
    ORDER_BLOCK,
    PATTERN,
    RSI,
    STRUCTURE,
    TREND,
    VOLUME,
    VWAP,
)


@dataclass
class Narrative:
    """The dual-mode explanation of one signal."""

    summary: str
    professional: List[str] = field(default_factory=list)
    beginner: List[str] = field(default_factory=list)


def build_narrative(ctx: SignalContext) -> Narrative:
    """Compose the summary and the beginner / professional bullet lists."""
    if not ctx.actionable:
        return _wait_narrative(ctx)

    direction = ctx.signal.direction
    side = ctx.side
    confirmed = _confirmed_votes(ctx, side)

    professional = [f'{v.label} — {v.detail}' for v in confirmed]
    beginner = [_beginner_line(v.module, direction) for v in confirmed]
    beginner = [line for line in beginner if line]

    return Narrative(
        summary=_summary(ctx, side, confirmed),
        professional=professional or ['Setup confirmed across multiple modules.'],
        beginner=beginner or ['This setup has confirmation from several signals.'],
    )


# ─────────────────────────────────────────────
# Summary paragraph
# ─────────────────────────────────────────────

def _summary(ctx: SignalContext, side: str, confirmed) -> str:
    """A short professional paragraph, assembled from what confirmed."""
    signal = ctx.signal
    parts: List[str] = []
    direction_word = 'bullish' if side == 'long' else 'bearish'
    htf = ctx.mtf.higher_timeframe_direction

    # Sentence 1 — the higher-timeframe backdrop.
    if htf in ('bullish', 'bearish'):
        parts.append(f'The higher timeframe trend is {htf}.')

    # Sentence 2 — the structural / wave story.
    modules = {v.module for v in confirmed}
    story: List[str] = []
    if STRUCTURE in modules:
        story.append('market structure confirms the direction')
    if ELLIOTT in modules:
        wave = ctx.picture.elliott.label
        story.append(f'price is unfolding a {wave} continuation')
    if ORDER_BLOCK in modules:
        story.append('price is reacting from an order block')
    if FIBONACCI in modules and ctx.picture.fibonacci.in_golden_pocket:
        story.append('inside the Fibonacci golden pocket')
    if story:
        parts.append('The setup shows ' + ', '.join(story) + '.')

    # Sentence 3 — momentum / participation.
    momentum: List[str] = []
    if VOLUME in modules:
        momentum.append('volume confirms participation')
    if RSI in modules:
        momentum.append('RSI is healthy')
    if MACD in modules:
        momentum.append('MACD momentum agrees')
    if momentum:
        parts.append(_capitalise(', '.join(momentum)) + '.')

    # Sentence 4 — the plan.
    if signal.take_profits:
        parts.append(
            f'The overall picture supports {direction_word} continuation toward the '
            f'projected targets, with roughly {signal.risk_reward:.1f}:1 reward to risk.'
        )

    return ' '.join(parts) or (
        f'A {direction_word} setup confirmed by the confluence of modules.'
    )


# ─────────────────────────────────────────────
# Beginner language
# ─────────────────────────────────────────────

_BEGINNER_BULLISH = {
    TREND: 'The bigger picture is pointing up.',
    STRUCTURE: 'The market is making higher highs and higher lows.',
    ELLIOTT: 'The upward move looks like it has more room to run.',
    FIBONACCI: 'Price bounced from an important level buyers watch.',
    VOLUME: 'Plenty of buyers are backing this move.',
    RSI: 'Momentum is positive but not overheated.',
    ORDER_BLOCK: 'Price reacted from a spot where big buyers stepped in before.',
    FVG: 'There is an imbalance below that tends to support price.',
    LIQUIDITY: 'Sellers were trapped and buyers took control.',
    VWAP: 'Price is trading above its average — buyers are in control.',
    MACD: 'A momentum indicator just turned positive.',
    PATTERN: 'A recognisable bullish chart pattern is forming.',
}

_BEGINNER_BEARISH = {
    TREND: 'The bigger picture is pointing down.',
    STRUCTURE: 'The market is making lower highs and lower lows.',
    ELLIOTT: 'The downward move looks like it has more room to run.',
    FIBONACCI: 'Price rejected an important level sellers watch.',
    VOLUME: 'Plenty of sellers are backing this move.',
    RSI: 'Momentum is negative but not oversold yet.',
    ORDER_BLOCK: 'Price reacted from a spot where big sellers stepped in before.',
    FVG: 'There is an imbalance above that tends to cap price.',
    LIQUIDITY: 'Buyers were trapped and sellers took control.',
    VWAP: 'Price is trading below its average — sellers are in control.',
    MACD: 'A momentum indicator just turned negative.',
    PATTERN: 'A recognisable bearish chart pattern is forming.',
}


def _beginner_line(module: str, direction: str) -> str:
    table = _BEGINNER_BULLISH if direction == 'BUY' else _BEGINNER_BEARISH
    return table.get(module, '')


# ─────────────────────────────────────────────
# WAIT
# ─────────────────────────────────────────────

def _wait_narrative(ctx: SignalContext) -> Narrative:
    reason = _sentence(ctx.signal.wait_reason or 'No qualifying setup')
    lean = ctx.confluence.direction
    summary = (
        f'No trade right now. {reason} '
        + (f'The evidence currently leans {lean}, but it has not met every '
           'requirement for a signal.' if lean in ('bullish', 'bearish') else
           'The market has no clear direction on this timeframe.')
    )
    professional = [f'WAIT — {reason}']
    professional += [f'Conflict: {c}' for c in ctx.confluence.hard_conflicts[:3]]
    beginner = [
        'There is no clear trade here yet.',
        'It is safer to wait than to force an entry.',
        'Check again after the next candle closes.',
    ]
    return Narrative(summary=summary, professional=professional, beginner=beginner)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _confirmed_votes(ctx: SignalContext, side):
    """The votes that confirm the direction, strongest first."""
    votes = [
        v for v in ctx.confluence.votes
        if side and v.agrees_with(side) and v.strength > 0
    ]
    return sorted(votes, key=lambda v: v.strength, reverse=True)


def _capitalise(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _sentence(text: str) -> str:
    """Ensure a fragment reads as a sentence (capitalised, ends with a period)."""
    text = _capitalise(text.strip())
    if text and text[-1] not in '.!?':
        text += '.'
    return text
