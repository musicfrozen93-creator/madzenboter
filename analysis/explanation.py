"""Signal Explanation Engine — analysis results rendered as plain English.

This is NOT AI. It is a deterministic template layer: every line it emits is a
direct restatement of a module vote, a score component, or a level the generator
chose. Same analysis in, same sentences out.

The goal is that a user reading the reasons understands exactly why the signal
exists — and, when the answer is WAIT, exactly what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from analysis.confluence import ConfluenceResult
from analysis.generator import BUY, SELL, WAIT, TradingSignal
from analysis.modules import (
    ADX,
    ATR,
    ELLIOTT,
    FIBONACCI,
    FVG,
    LIQUIDITY,
    MACD,
    MODULE_ORDER,
    NEUTRAL_VOTE,
    ORDER_BLOCK,
    PATTERN,
    RSI,
    STRUCTURE,
    SUPPORT_RESISTANCE,
    TREND,
    VOLUME,
    VWAP,
    ModuleVote,
)
from analysis.scoring import Score
from analysis.timeframes import MultiTimeframePicture

CONFIRM = '✓'
AGAINST = '✕'
NEUTRAL = '•'


@dataclass
class Explanation:
    """The human-readable account of one signal."""

    headline: str
    supporting: List[str]        # reasons the signal exists
    against: List[str]           # evidence pointing the other way
    notes: List[str]             # context that is neither for nor against

    @property
    def all_reasons(self) -> List[str]:
        """Every line, prefixed with its stance, for a single-list display."""
        return (
            [f'{CONFIRM} {line}' for line in self.supporting]
            + [f'{AGAINST} {line}' for line in self.against]
            + [f'{NEUTRAL} {line}' for line in self.notes]
        )


class ExplanationEngine:
    """Renders module votes and chosen levels into readable reasons."""

    def build(
        self,
        signal: TradingSignal,
        mtf: MultiTimeframePicture,
        confluence: ConfluenceResult,
        quality: Score,
        confidence: Score,
    ) -> Explanation:
        """Produce the full explanation for a signal."""
        if signal.direction == WAIT:
            return self._explain_wait(signal, mtf, confluence, quality)
        return self._explain_trade(signal, mtf, confluence, quality, confidence)

    # ── Actionable signals ──

    def _explain_trade(
        self,
        signal: TradingSignal,
        mtf: MultiTimeframePicture,
        confluence: ConfluenceResult,
        quality: Score,
        confidence: Score,
    ) -> Explanation:
        side = 'long' if signal.direction == BUY else 'short'
        supporting: List[str] = []
        against: List[str] = []
        notes: List[str] = []

        for module in MODULE_ORDER:
            vote = confluence.vote(module)
            if vote is None:
                continue
            sentence = _sentence(vote, mtf)
            if module == ATR:
                notes.append(sentence)
            elif vote.agrees_with(side):
                supporting.append(sentence)
            elif vote.direction == NEUTRAL_VOTE:
                notes.append(sentence)
            else:
                against.append(sentence)

        # Levels — the reasoning behind the numbers the user will act on.
        if signal.entry_basis:
            notes.append(f'Entry taken at the {signal.entry_basis}')
        if signal.stop_basis:
            notes.append(f'Stop placed {signal.stop_basis}')
        for index, source in enumerate(signal.target_sources, start=1):
            notes.append(f'TP{index} sits at the {source}')
        if signal.risk_reward:
            notes.append(
                f'Reward:risk of {signal.risk_reward:.2f} to TP3 '
                f'with {signal.risk_pct:.2%} risk to the stop'
            )

        headline = (
            f'{signal.direction} {signal.symbol} on {signal.timeframe} — '
            f'{quality.grade} setup ({quality.value}/100), '
            f'{confidence.grade.lower()} confidence ({confidence.value}/100)'
        )
        return Explanation(headline, supporting, against, notes)

    # ── WAIT ──

    def _explain_wait(
        self,
        signal: TradingSignal,
        mtf: MultiTimeframePicture,
        confluence: ConfluenceResult,
        quality: Score,
    ) -> Explanation:
        """Explain what is missing, not just that something is."""
        supporting: List[str] = []
        against: List[str] = []
        notes: List[str] = []

        # The blocking reason always comes first.
        against.append(signal.wait_reason or 'no qualifying setup')

        leaning = confluence.trade_side
        for module in MODULE_ORDER:
            vote = confluence.vote(module)
            if vote is None:
                continue
            sentence = _sentence(vote, mtf)
            if module == ATR or vote.direction == NEUTRAL_VOTE:
                notes.append(sentence)
            elif leaning and vote.agrees_with(leaning):
                supporting.append(sentence)
            elif leaning:
                against.append(sentence)
            else:
                notes.append(sentence)

        if confluence.has_direction:
            notes.append(
                f'Evidence currently leans {confluence.direction} '
                f'({confluence.agreement:.0%} of cast module weight), but the '
                f'setup did not clear every requirement'
            )
        notes.append(f'Setup quality would be {quality.value}/100 ({quality.grade})')

        headline = (
            f'WAIT on {signal.symbol} {signal.timeframe} — '
            f'{signal.wait_reason or "no qualifying setup"}'
        )
        return Explanation(headline, supporting, against, notes)


# ─────────────────────────────────────────────
# Sentence templates
# ─────────────────────────────────────────────

def _sentence(vote: ModuleVote, mtf: MultiTimeframePicture) -> str:
    """Render one module vote as a sentence a trader would recognise."""
    if vote.module == TREND:
        if vote.direction == NEUTRAL_VOTE:
            return 'Higher timeframe trend is unclear'
        scope = 'Higher timeframe trend' if mtf.ladder.is_multi else 'Trend'
        return f'{scope} {vote.direction} ({vote.detail})'

    if vote.module == STRUCTURE:
        if vote.direction == NEUTRAL_VOTE:
            return f'Market structure is undirected — {vote.label.lower()}'
        return f'{vote.label} — {vote.detail}'

    if vote.module == ELLIOTT:
        if vote.direction == NEUTRAL_VOTE:
            return 'Elliott wave count could not be established'
        return f'Elliott {vote.label} {vote.direction} — {vote.detail}'

    if vote.module == FIBONACCI:
        if vote.direction == NEUTRAL_VOTE:
            return 'No measurable Fibonacci leg'
        if vote.label == 'Golden Pocket':
            return 'Price inside the golden pocket (0.618–0.65 retracement)'
        return f'Fibonacci: {vote.label}'

    if vote.module == VOLUME:
        return f'{vote.label} relative volume — {vote.detail}'

    if vote.module == RSI:
        return f'RSI {vote.label.lower()} — {vote.detail}'

    if vote.module == ATR:
        return f'Volatility {vote.label.lower()} — {vote.detail}'

    if vote.module == SUPPORT_RESISTANCE:
        if vote.direction == NEUTRAL_VOTE:
            return f'Support/resistance: {vote.label.lower()} — {vote.detail}'
        return f'{vote.label} — {vote.detail}'

    if vote.module == ORDER_BLOCK:
        if vote.direction == NEUTRAL_VOTE:
            return 'No active order block near price'
        return f'{vote.label} — {vote.detail}'

    if vote.module == FVG:
        if vote.direction == NEUTRAL_VOTE:
            return 'No unfilled fair value gap near price'
        return f'{vote.label} — {vote.detail}'

    if vote.module == LIQUIDITY:
        if vote.direction == NEUTRAL_VOTE:
            return f'Liquidity: {vote.detail}'
        return f'{vote.label} — {vote.detail}'

    if vote.module == VWAP:
        if vote.direction == NEUTRAL_VOTE:
            return f'Price at VWAP — {vote.detail}'
        return f'{vote.label} — {vote.detail}'

    if vote.module == MACD:
        if vote.direction == NEUTRAL_VOTE:
            return 'MACD flat'
        return f'MACD {vote.label.lower()} — {vote.detail}'

    if vote.module == ADX:
        return f'{vote.label} — {vote.detail}'

    if vote.module == PATTERN:
        if vote.direction == NEUTRAL_VOTE:
            return f'Pattern: {vote.label}' if vote.label != 'None' else 'No chart pattern detected'
        return f'{vote.label} pattern — {vote.detail}'

    return f'{vote.module}: {vote.label}'
