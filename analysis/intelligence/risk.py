"""Risk Advisory Engine.

Summarises the risk in the setup as Low / Medium / High with the specific
reasons behind it. Every reason is read from the actual analysis — a nearby
opposing level, elevated ATR, a counter-trend direction, thin volume, a wide
stop, weak momentum — never a generic warning.

Favourable conditions (a strong aligned trend, a tight stop) are reported too,
because they legitimately lower the risk read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from analysis.intelligence.context import SignalContext
from analysis.structure import BEARISH, BULLISH

LOW = 'Low'
MEDIUM = 'Medium'
HIGH = 'High'

# A stop wider than this fraction of price is a meaningful risk factor.
WIDE_STOP_PCT = 0.03


@dataclass(frozen=True)
class RiskFactor:
    """One reason contributing to (or reducing) the risk read."""

    factor: str
    raises_risk: bool
    detail: str


@dataclass
class RiskAdvisory:
    """The risk summary of one signal."""

    level: str                          # 'Low' | 'Medium' | 'High'
    score: int                          # net risk points (higher = riskier)
    factors: List[RiskFactor] = field(default_factory=list)
    summary: str = ''

    @property
    def raising(self) -> List[RiskFactor]:
        return [f for f in self.factors if f.raises_risk]

    @property
    def reducing(self) -> List[RiskFactor]:
        return [f for f in self.factors if not f.raises_risk]


def assess_risk(ctx: SignalContext) -> RiskAdvisory:
    """Build the risk advisory from the analysis.

    Risk points accrue from each adverse condition and are reduced by favourable
    ones. The net maps to Low (<2), Medium (2–3), or High (≥4).
    """
    picture = ctx.picture
    ind = picture.indicators
    signal = ctx.signal
    side = ctx.side
    factors: List[RiskFactor] = []
    points = 0

    # ── Counter-trend to the higher timeframe ──
    htf = ctx.mtf.higher_timeframe_direction
    if side and htf in (BULLISH, BEARISH):
        wanted = BULLISH if side == 'long' else BEARISH
        if htf != wanted:
            points += 2
            factors.append(RiskFactor(
                'Counter Trend', True,
                f'The higher timeframe is {htf}, against a {side} setup.',
            ))
        else:
            points -= 1
            factors.append(RiskFactor(
                'Strong Trend', False,
                f'The higher timeframe trend is {htf}, aligned with the setup.',
            ))

    # ── Volatility (ATR) ──
    if ind.volatility_label == 'elevated':
        points += 2
        factors.append(RiskFactor(
            'High ATR', True,
            f'Volatility is elevated ({ind.atr_pct:.2%} vs {ind.atr_average_pct:.2%} '
            'average) — expect wider swings.',
        ))
    elif ind.volatility_label == 'compressed':
        points += 1
        factors.append(RiskFactor(
            'Compressed Volatility', True,
            'Volatility is compressed — a sharp expansion may follow.',
        ))

    # ── Wide stop ──
    if signal.risk_pct is not None and signal.risk_pct > WIDE_STOP_PCT:
        points += 1
        factors.append(RiskFactor(
            'Wide Stop', True,
            f'The stop sits {signal.risk_pct:.2%} away — a larger loss if hit.',
        ))

    # ── Nearby opposing level ──
    if signal.actionable:
        levels = picture.levels
        obstacle = levels.nearest_resistance if side == 'long' else levels.nearest_support
        if obstacle is not None and ind.atr > 0:
            room = abs(obstacle.center - ind.price) / ind.atr
            if room < 1.5:
                points += 2
                word = 'resistance' if side == 'long' else 'support'
                factors.append(RiskFactor(
                    f'Nearby {word.title()}', True,
                    f'{word.title()} at {obstacle.center:g} is only {room:.1f} ATR away.',
                ))

    # ── Volume ──
    volume_vote = ctx.confluence.vote('volume')
    if volume_vote is not None and volume_vote.label == 'Weak':
        points += 1
        factors.append(RiskFactor(
            'Low Volume', True,
            'Participation is thin — the move lacks conviction.',
        ))
    elif volume_vote is not None and volume_vote.label in ('Strong', 'Above average') \
            and side and volume_vote.agrees_with(side):
        points -= 1
        factors.append(RiskFactor(
            'Volume Support', False,
            f'{volume_vote.detail} supports the move.',
        ))

    # ── Momentum ──
    rsi_vote = ctx.confluence.vote('rsi')
    macd_vote = ctx.confluence.vote('macd')
    weak_rsi = rsi_vote is not None and rsi_vote.label in ('Neutral', 'Overbought', 'Oversold')
    weak_macd = macd_vote is not None and macd_vote.strength < 0.5
    if weak_rsi and weak_macd:
        points += 1
        factors.append(RiskFactor(
            'Weak Momentum', True,
            'RSI and MACD are not strongly confirming the direction.',
        ))
    elif side and macd_vote is not None and macd_vote.agrees_with(side) and macd_vote.strength >= 0.9:
        points -= 1
        factors.append(RiskFactor(
            'Momentum Confirmation', False,
            f'{macd_vote.detail} confirms the direction.',
        ))

    # ── Low agreement ──
    if ctx.confluence.agreement and ctx.confluence.agreement < 0.7:
        points += 1
        factors.append(RiskFactor(
            'Mixed Confluence', True,
            f'Only {ctx.confluence.agreement:.0%} of the evidence is one-sided.',
        ))

    level = HIGH if points >= 4 else MEDIUM if points >= 2 else LOW
    raising = [f.factor for f in factors if f.raises_risk]
    summary = (
        f'{level} risk' + (f' — {", ".join(raising)}.' if raising else ' — no major risk flags.')
    )

    return RiskAdvisory(level=level, score=points, factors=factors, summary=summary)
