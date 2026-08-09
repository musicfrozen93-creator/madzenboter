"""Signal Lifecycle.

Describes where the signal sits in its life at the moment of analysis, and when
it expires. This is a stateless snapshot — the platform does not track live
positions, so status is inferred from the current price versus the entry, and
the execution timeline is presented as the stages the trade WOULD pass through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from analysis.intelligence.context import SignalContext

# Lifecycle statuses.
WAITING = 'waiting'          # actionable, price not yet at the entry
TRIGGERED = 'triggered'      # price has reached the entry zone
NO_SETUP = 'no_setup'        # WAIT — nothing to trade

# Price within this fraction of the risk distance counts as "at" the entry.
ENTRY_TOLERANCE = 0.25


@dataclass
class Lifecycle:
    """The lifecycle snapshot of one signal."""

    status: str                          # 'waiting' | 'triggered' | 'no_setup'
    status_label: str
    expiration: str
    recommend_fresh_analysis: bool
    stages: List[str] = field(default_factory=list)
    current_stage: Optional[str] = None
    detail: str = ''


# The full execution sequence an actionable signal passes through.
EXECUTION_STAGES = (
    'Waiting',
    'Entry Trigger',
    'Trade Active',
    'TP1',
    'Move SL to Break Even',
    'TP2',
    'Trail Stop Loss',
    'TP3',
    'Trade Complete',
)


def build_lifecycle(ctx: SignalContext) -> Lifecycle:
    """Determine the signal's current lifecycle stage and expiration."""
    signal = ctx.signal
    timeframe = signal.timeframe
    expiration = f'Valid until the next {timeframe} candle closes'

    if not ctx.actionable:
        return Lifecycle(
            status=NO_SETUP,
            status_label='No Setup',
            expiration=expiration,
            recommend_fresh_analysis=True,
            stages=list(EXECUTION_STAGES),
            current_stage=None,
            detail=(
                'No tradeable setup on this timeframe right now. Re-run the '
                f'analysis after the next {timeframe} candle closes.'
            ),
        )

    # Has price already reached the entry zone?
    price = ctx.picture.indicators.price
    entry = signal.entry
    risk = abs(entry - signal.stop_loss) if signal.stop_loss is not None else 0.0
    tolerance = ENTRY_TOLERANCE * risk if risk > 0 else 0.0
    triggered = abs(price - entry) <= tolerance

    if triggered:
        return Lifecycle(
            status=TRIGGERED,
            status_label='Triggered',
            expiration=expiration,
            recommend_fresh_analysis=False,
            stages=list(EXECUTION_STAGES),
            current_stage='Entry Trigger',
            detail=(
                f'Price is inside the entry zone ({_fmt(entry)}). Wait for the '
                f'confirmation candle before committing.'
            ),
        )

    return Lifecycle(
        status=WAITING,
        status_label='Waiting for Entry',
        expiration=expiration,
        recommend_fresh_analysis=False,
        stages=list(EXECUTION_STAGES),
        current_stage='Waiting',
        detail=(
            f'Price ({_fmt(price)}) has not yet reached the entry zone '
            f'({_fmt(entry)}). If it expires before entry, run a fresh analysis.'
        ),
    )


def _fmt(value) -> str:
    return '—' if value is None else f'{value:g}'
