"""Signal Health Engine.

A single at-a-glance rating of the overall setup, blending the three numbers the
Analysis Engine already produced — Quality, Confidence, and the Validation score
— into a 1–5 star health rating. It computes nothing new about the market; it
summarises what the engine already decided.
"""

from __future__ import annotations

from dataclasses import dataclass

from analysis.intelligence.context import SignalContext
from analysis.intelligence.validation import ValidationResult

# How the composite is weighted. Quality (setup goodness) leads; confidence and
# validation temper it.
QUALITY_WEIGHT = 0.40
CONFIDENCE_WEIGHT = 0.30
VALIDATION_WEIGHT = 0.30

# Composite → (stars, label). Checked high to low.
HEALTH_BANDS = (
    (85, 5, 'Excellent'),
    (70, 4, 'Good'),
    (55, 3, 'Average'),
    (40, 2, 'Weak'),
    (0, 1, 'Poor'),
)


@dataclass
class HealthResult:
    """The overall health rating of a signal."""

    stars: int             # 1–5
    stars_display: str     # '★★★★☆'
    label: str             # 'Excellent' … 'Poor'
    composite: int         # 0–100 blended score
    quality: int
    confidence: int
    validation: int
    summary: str


def rate_health(ctx: SignalContext, validation: ValidationResult) -> HealthResult:
    """Blend quality, confidence, and validation into a star rating.

    A WAIT signal is capped at two stars: however clean the individual readings,
    a setup the engine declined to trade is not a healthy trade.
    """
    quality = ctx.quality.value
    confidence = ctx.confidence.value
    val = validation.score

    composite = int(round(
        QUALITY_WEIGHT * quality
        + CONFIDENCE_WEIGHT * confidence
        + VALIDATION_WEIGHT * val
    ))
    composite = max(0, min(100, composite))

    stars, label = _band(composite)

    if ctx.is_wait:
        # No tradeable setup — never present it as a healthy trade.
        stars = min(stars, 2)
        _, label = _label_for_stars(stars)

    return HealthResult(
        stars=stars,
        stars_display='★' * stars + '☆' * (5 - stars),
        label=label,
        composite=composite,
        quality=quality,
        confidence=confidence,
        validation=val,
        summary=(
            f'{label} health — quality {quality}, confidence {confidence}, '
            f'validation {val} (blended {composite}/100)'
        ),
    )


def _band(composite: int) -> tuple[int, str]:
    for threshold, stars, label in HEALTH_BANDS:
        if composite >= threshold:
            return stars, label
    return 1, 'Poor'


def _label_for_stars(stars: int) -> tuple[int, str]:
    for _, s, label in HEALTH_BANDS:
        if s == stars:
            return stars, label
    return stars, 'Poor'
