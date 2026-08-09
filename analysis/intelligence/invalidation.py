"""Invalidation Engine.

Lists the exact conditions that would invalidate the trade — each one generated
from a module that actually participated in the signal, referencing the real
level involved. Nothing is invented: a condition appears only when the analysis
that produced it is present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from analysis.intelligence.context import SignalContext
from analysis.smc.liquidity import BUY_SIDE


@dataclass(frozen=True)
class InvalidationCondition:
    """One condition that would void the setup."""

    source: str            # which module it comes from
    condition: str


@dataclass
class Invalidation:
    """The set of conditions that invalidate a signal."""

    conditions: List[InvalidationCondition] = field(default_factory=list)
    summary: str = ''


def build_invalidation(ctx: SignalContext) -> Invalidation:
    """Derive the invalidation conditions from the analysis.

    For a WAIT there is no live trade to invalidate, so the engine instead
    describes what would need to change for a setup to appear.
    """
    if not ctx.actionable:
        return _wait_conditions(ctx)

    picture = ctx.picture
    signal = ctx.signal
    long = signal.direction == 'BUY'
    conditions: List[InvalidationCondition] = []

    # ── The stop itself — the master invalidation ──
    conditions.append(InvalidationCondition(
        'stop', f'Price closes beyond the stop loss at {_fmt(signal.stop_loss)}.',
    ))

    # ── Structure ──
    structure = picture.structure
    protective = structure.protective_low if long else structure.protective_high
    if protective is not None:
        side_word = 'below' if long else 'above'
        conditions.append(InvalidationCondition(
            'structure',
            f'Price closes {side_word} the protective swing at {_fmt(protective)} '
            '(the market structure breaks).',
        ))
    if structure.event == 'bos' and structure.event_direction:
        conditions.append(InvalidationCondition(
            'structure',
            f'The {structure.event_direction} break of structure fails and price '
            'closes back through it.',
        ))

    # ── Order block ──
    ob = picture.order_blocks
    if ob.nearest is not None and ob.direction != 'neutral':
        conditions.append(InvalidationCondition(
            'order_block',
            f'The {ob.direction} order block ({_fmt(ob.nearest.bottom)}–'
            f'{_fmt(ob.nearest.top)}) is lost — price closes through it.',
        ))

    # ── Liquidity ──
    liq = picture.liquidity
    if liq.last_sweep is not None:
        reclaim = 'reclaims' if liq.last_sweep.side == BUY_SIDE else 'loses'
        conditions.append(InvalidationCondition(
            'liquidity',
            f'The liquidity sweep is rejected — price {reclaim} '
            f'{_fmt(liq.last_sweep.price)} instead of continuing.',
        ))

    # ── Fair value gap ──
    fvg = picture.fair_value_gaps
    if fvg.nearest is not None and fvg.direction != 'neutral':
        conditions.append(InvalidationCondition(
            'fvg',
            f'Price fully fills back through the {fvg.direction} fair value gap '
            f'({_fmt(fvg.nearest.bottom)}–{_fmt(fvg.nearest.top)}).',
        ))

    # ── MACD ──
    macd = picture.macd
    opposite_cross = 'bearish' if long else 'bullish'
    conditions.append(InvalidationCondition(
        'macd', f'A {opposite_cross} MACD crossover appears.',
    ))

    # ── RSI momentum ──
    conditions.append(InvalidationCondition(
        'rsi',
        'RSI loses momentum and crosses back through 50 against the trade.',
    ))

    # ── Volume ──
    conditions.append(InvalidationCondition(
        'volume', 'Volume collapses below its average as price moves in favour.',
    ))

    return Invalidation(
        conditions=conditions,
        summary=f'This {signal.direction} signal becomes invalid if any of '
                f'{len(conditions)} conditions occur.',
    )


def _wait_conditions(ctx: SignalContext) -> Invalidation:
    """For a WAIT, describe what would need to change for a setup to appear."""
    conditions = [
        InvalidationCondition('wait', ctx.signal.wait_reason or 'No qualifying setup.'),
    ]
    for note in ctx.confluence.hard_conflicts[:3]:
        conditions.append(InvalidationCondition('conflict', f'Resolve: {note}.'))
    return Invalidation(
        conditions=conditions,
        summary='No live trade to invalidate — these are the blockers a setup '
                'would need to clear.',
    )


def _fmt(value) -> str:
    return '—' if value is None else f'{value:g}'
