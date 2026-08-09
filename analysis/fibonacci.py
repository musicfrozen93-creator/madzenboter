"""Fibonacci — retracements, the golden pocket, and extension targets.

The dominant impulse leg is taken from the two most recent confirmed swings.
Retracements are measured across that leg; extensions are projected beyond it and
become the raw material for take-profit levels (so targets come from the actual
move, never from a fixed multiplier).

The golden pocket — the 0.618–0.65 retracement band — is where continuation
entries are conventionally sought, so it is reported explicitly.

Pure and network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from analysis.structure import Swing

# Retracement ratios measured back across the impulse leg.
RETRACEMENT_RATIOS: tuple[float, ...] = (0.236, 0.382, 0.5, 0.618, 0.65, 0.786)

# Extension ratios projected beyond the leg, used for targets.
EXTENSION_RATIOS: tuple[float, ...] = (1.272, 1.414, 1.618, 2.0, 2.618)

# The golden pocket band.
GOLDEN_POCKET = (0.618, 0.65)

# A retracement deeper than this invalidates the leg as a continuation setup.
INVALIDATION_RATIO = 1.0


@dataclass
class FibonacciState:
    """Fibonacci geometry of the current dominant leg."""

    valid: bool = False
    direction: Optional[str] = None        # 'up' | 'down' (leg direction)
    leg_start: float = 0.0                 # origin of the impulse
    leg_end: float = 0.0                   # extreme of the impulse
    leg_size: float = 0.0
    retracements: Dict[str, float] = field(default_factory=dict)
    extensions: Dict[str, float] = field(default_factory=dict)
    current_ratio: float = 0.0             # where price sits across the leg
    in_golden_pocket: bool = False
    zone_label: str = 'no leg detected'
    reason: str = 'insufficient_swings'

    def supports(self, direction: str) -> bool:
        """True when the leg direction agrees with a proposed trade direction."""
        if not self.valid:
            return False
        return self.direction == ('up' if direction == 'long' else 'down')

    def extension_targets(self, direction: str) -> List[float]:
        """Extension prices ordered away from entry for a trade direction."""
        if not self.extensions:
            return []
        prices = sorted(self.extensions.values())
        return prices if direction == 'long' else sorted(prices, reverse=True)


def analyze_fibonacci(swings: List[Swing], price: float) -> FibonacciState:
    """Measure the current dominant leg and its Fibonacci geometry.

    The leg runs from the second-to-last confirmed swing to the last one, which
    is by construction the most recent completed directional move.

    Args:
        swings: Alternating confirmed swings, oldest first.
        price: Current price.

    Returns:
        A FibonacciState. `valid` is False when no measurable leg exists — the
        caller must not treat the zeroed fields as real levels.
    """
    if len(swings) < 2 or price <= 0:
        return FibonacciState()

    leg_end_swing = swings[-1]
    leg_start_swing = swings[-2]
    leg_start = leg_start_swing.price
    leg_end = leg_end_swing.price
    leg_size = abs(leg_end - leg_start)

    if leg_size <= 0:
        return FibonacciState(reason='degenerate_leg')

    direction = 'up' if leg_end > leg_start else 'down'
    sign = 1.0 if direction == 'up' else -1.0

    # Retracements measured back from the leg's extreme toward its origin.
    retracements = {
        _label(ratio): leg_end - sign * ratio * leg_size
        for ratio in RETRACEMENT_RATIOS
    }
    # Extensions projected beyond the extreme, in the leg's direction.
    extensions = {
        _label(ratio): leg_start + sign * ratio * leg_size
        for ratio in EXTENSION_RATIOS
    }

    # How far price has retraced across the leg: 0.0 at the extreme, 1.0 back at
    # the origin, >1.0 once the leg is fully invalidated.
    #
    # SIGNED, deliberately. Price that has travelled FURTHER in the leg's own
    # direction gives a negative ratio — that is an extension, the opposite of a
    # retracement. Taking the absolute value here would report a runaway trend
    # as a deep pullback and invert the module's vote.
    current_ratio = (
        (leg_end - price) / leg_size if direction == 'up'
        else (price - leg_end) / leg_size
    )
    in_golden_pocket = GOLDEN_POCKET[0] <= current_ratio <= GOLDEN_POCKET[1]

    return FibonacciState(
        valid=True,
        direction=direction,
        leg_start=leg_start,
        leg_end=leg_end,
        leg_size=leg_size,
        retracements=retracements,
        extensions=extensions,
        current_ratio=round(current_ratio, 4),
        in_golden_pocket=in_golden_pocket,
        zone_label=_zone_label(current_ratio, in_golden_pocket),
        reason=(
            f'{direction} leg {leg_start:.8f} → {leg_end:.8f}, price at '
            + (
                f'{-current_ratio:.1%} extension beyond it'
                if current_ratio < 0 else f'{current_ratio:.1%} retracement'
            )
        ),
    )


def _label(ratio: float) -> str:
    """Render a ratio as a stable dictionary key ('0.618', '1.618')."""
    return f'{ratio:g}'


def _zone_label(ratio: float, in_golden_pocket: bool) -> str:
    """Name the retracement band price currently occupies."""
    if in_golden_pocket:
        return 'golden pocket (0.618–0.65)'
    if ratio < 0:
        return f'extended {-ratio:.0%} beyond the leg'
    if ratio <= 0.236:
        return 'shallow (0–0.236)'
    if ratio <= 0.382:
        return 'shallow (0.236–0.382)'
    if ratio <= 0.5:
        return 'moderate (0.382–0.5)'
    if ratio <= 0.618:
        return 'deep (0.5–0.618)'
    if ratio <= 0.786:
        return 'deep (0.65–0.786)'
    if ratio <= INVALIDATION_RATIO:
        return 'very deep (0.786–1.0)'
    return 'leg invalidated (>1.0)'
