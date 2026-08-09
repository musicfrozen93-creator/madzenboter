"""Shared read-only context for the trade-intelligence engines.

`SignalContext` bundles the already-computed analysis so every intelligence
engine reads from ONE snapshot and recomputes nothing. It is deliberately a
read-only view: the intelligence layer reviews and explains the signal the
Analysis Engine produced — it never alters the direction, entry, stop, targets,
quality, or confidence, and it never generates a signal of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from analysis.confluence import ConfluenceResult
from analysis.engine import TechnicalPicture
from analysis.generator import BUY, SELL, WAIT, TradingSignal
from analysis.scoring import Score
from analysis.timeframes import MultiTimeframePicture


@dataclass(frozen=True)
class SignalContext:
    """Everything the intelligence engines need, computed exactly once."""

    signal: TradingSignal
    mtf: MultiTimeframePicture
    confluence: ConfluenceResult
    quality: Score
    confidence: Score

    @property
    def picture(self) -> TechnicalPicture:
        """The selected timeframe's technical picture."""
        return self.mtf.entry

    @property
    def actionable(self) -> bool:
        return self.signal.direction in (BUY, SELL)

    @property
    def side(self) -> Optional[str]:
        """The trade side the intelligence is framed around.

        For an actionable signal it is that signal's side. For a WAIT it falls
        back to the direction the confluence leaned, so the validation and
        explanations can still describe what WOULD be needed — never inventing a
        trade, only reporting the lean.
        """
        if self.signal.direction == BUY:
            return 'long'
        if self.signal.direction == SELL:
            return 'short'
        return self.confluence.trade_side

    @property
    def is_wait(self) -> bool:
        return self.signal.direction == WAIT

    @classmethod
    def from_result(cls, result) -> 'SignalContext':
        """Build the context from a completed `AnalysisResult`."""
        return cls(
            signal=result.signal,
            mtf=result.mtf,
            confluence=result.confluence,
            quality=result.quality,
            confidence=result.confidence,
        )
