"""Trade Management Guide.

Turns the signal's actual levels into a step-by-step management plan. Every step
references the real entry zone, stop, and targets the Analysis Engine produced —
there is no generic advice. The guide is EDUCATIONAL only: the platform never
places, moves, or manages a live order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from analysis.intelligence.context import SignalContext


@dataclass(frozen=True)
class GuideStep:
    """One numbered step in the management plan."""

    number: int
    title: str
    detail: str


@dataclass
class TradeGuide:
    """The full management plan for one signal."""

    tradeable: bool
    steps: List[GuideStep] = field(default_factory=list)
    note: str = ''


def build_trade_guide(ctx: SignalContext) -> TradeGuide:
    """Build the management guide for the signal.

    For an actionable signal the steps walk from confirmation through each target
    with break-even and trailing management. For a WAIT there is no trade to
    manage, so the guide explains what to do instead.
    """
    signal = ctx.signal
    if not ctx.actionable:
        return TradeGuide(
            tradeable=False,
            steps=[
                GuideStep(1, 'No trade right now',
                          signal.wait_reason or 'No qualifying setup on this timeframe.'),
                GuideStep(2, 'Wait for the setup to develop',
                          'Let the current candle close before judging the market again.'),
                GuideStep(3, 'Re-run the analysis',
                          f'Analyse {signal.symbol} on {signal.timeframe} again after the '
                          'next candle closes rather than forcing an entry.'),
            ],
            note='This platform analyses markets and never places trades.',
        )

    direction = signal.direction
    entry = signal.entry
    stop = signal.stop_loss
    tps = signal.take_profits
    tp1, tp2, tp3 = (tps + [None, None, None])[:3]
    long = direction == 'BUY'
    move = 'above' if long else 'below'
    entry_word = 'long' if long else 'short'

    steps = [
        GuideStep(
            1, 'Wait for a confirmation candle',
            f'Let the current {signal.timeframe} candle close in favour of the '
            f'{entry_word} before committing — do not anticipate the entry.',
        ),
        GuideStep(
            2, 'Enter only inside the entry zone',
            f'Enter the {entry_word} near {_fmt(entry)} '
            f'({signal.entry_basis or "the entry level"}). '
            'If price runs away from the zone, skip the trade.',
        ),
        GuideStep(
            3, 'Risk only your predefined amount',
            f'Place the stop loss at {_fmt(stop)} '
            f'({signal.risk_pct * 100:.2f}% away). Size the position so this stop is '
            'your maximum planned loss, and never widen it.',
        ),
        GuideStep(
            4, 'Hold the stop until TP1',
            f'Do not move the stop loss before price reaches TP1 at {_fmt(tp1)}. '
            'Let the trade prove itself first.',
        ),
        GuideStep(
            5, 'At TP1 — move stop to break-even',
            f'When price hits TP1 ({_fmt(tp1)}), take partial profit and move the '
            f'stop loss to your entry at {_fmt(entry)}. The trade is now risk-free.',
        ),
        GuideStep(
            6, 'At TP2 — secure more profit',
            f'When price reaches TP2 ({_fmt(tp2)}), secure another portion and trail '
            f'the stop below the most recent {signal.timeframe} swing.',
        ),
        GuideStep(
            7, 'Let the runner reach TP3',
            f'Allow the remaining position to run toward TP3 ({_fmt(tp3)}) while '
            f'trailing the stop {move} each new swing. Exit fully at TP3 or when the '
            'trail is hit.',
        ),
    ]

    return TradeGuide(
        tradeable=True,
        steps=steps,
        note=(
            f'Reward-to-risk to TP3 is about {signal.risk_reward:.2f} : 1. '
            'This is a management guide only — you place and manage every order '
            'yourself.'
        ),
    )


def _fmt(value) -> str:
    """Render a price level, or a dash when a target is absent."""
    if value is None:
        return '—'
    return f'{value:g}'
