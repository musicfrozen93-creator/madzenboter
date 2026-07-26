"""Performance Learning (spec §12 — disciplined, gated, human-reviewed).

Turns realized statistics into RECOMMENDATIONS only. It never auto-applies a
change: every output is minimum-sample gated and flagged for human review, because
adapting to noise is worse than not adapting (spec §12 governance). Pooling trade
data across all accounts (identical strategy) reaches significance faster — the
caller supplies the pooled record set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from v4.analytics.analytics import TradingAnalytics
from v4.analytics.logger import TradeRecordV4


@dataclass(frozen=True)
class LearningReport:
    prune_symbols: List[str]
    avoid_sessions: List[str]
    confluence_win_rates: Dict[str, float]
    notes: List[str]
    requires_human_review: bool = True


class PerformanceLearning:
    def __init__(
        self,
        min_sample: int = 30,
        expectancy_prune_threshold: float = -0.05,
    ) -> None:
        # A dimension is only actionable once it has >= min_sample trades.
        self.min_sample = min_sample
        self.expectancy_prune_threshold = expectancy_prune_threshold

    def analyze(self, records: List[TradeRecordV4]) -> LearningReport:
        a = TradingAnalytics(records)
        notes: List[str] = []

        prune_symbols: List[str] = []
        for sym, s in a.by_symbol().items():
            if s.count >= self.min_sample and s.expectancy_r < self.expectancy_prune_threshold:
                prune_symbols.append(sym)
                notes.append(
                    f'symbol {sym}: {s.count} trades, expectancy {s.expectancy_r:.2f}R '
                    f'< {self.expectancy_prune_threshold:.2f}R → prune candidate'
                )
            elif s.count < self.min_sample:
                notes.append(f'symbol {sym}: only {s.count}/{self.min_sample} trades — insufficient sample, no action')

        avoid_sessions: List[str] = []
        for sess, s in a.by_session().items():
            if s.count >= self.min_sample and s.expectancy_r < self.expectancy_prune_threshold:
                avoid_sessions.append(sess)
                notes.append(
                    f'session {sess}: {s.count} trades, expectancy {s.expectancy_r:.2f}R → avoid candidate'
                )

        # Confluence calibration: realized win-rate per confluence bucket (only
        # reported where the sample is sufficient) — feeds a future score tilt.
        confluence_win_rates: Dict[str, float] = {}
        for bucket, s in a.by_confluence().items():
            if s.count >= self.min_sample:
                confluence_win_rates[bucket] = s.win_rate

        return LearningReport(
            prune_symbols=sorted(prune_symbols),
            avoid_sessions=sorted(avoid_sessions),
            confluence_win_rates=confluence_win_rates,
            notes=notes,
            requires_human_review=True,
        )
