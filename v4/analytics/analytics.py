"""Trading Analytics (spec §12).

Computes expectancy / win-rate / PnL statistics grouped by any dimension of the
recorded trades. Pure functions over a list of TradeRecordV4 — no state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List

from v4.analytics.logger import TradeRecordV4


@dataclass(frozen=True)
class GroupStats:
    key: str
    count: int
    wins: int
    losses: int
    win_rate: float
    expectancy_r: float      # mean R-multiple (the headline edge metric)
    avg_win_r: float
    avg_loss_r: float        # negative
    total_pnl: float
    avg_pnl: float


def _stats(key: str, recs: List[TradeRecordV4]) -> GroupStats:
    n = len(recs)
    if n == 0:
        return GroupStats(key, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = [r for r in recs if r.pnl > 0]
    losses = [r for r in recs if r.pnl < 0]
    total_pnl = sum(r.pnl for r in recs)
    expectancy_r = sum(r.r_multiple for r in recs) / n
    avg_win_r = (sum(r.r_multiple for r in wins) / len(wins)) if wins else 0.0
    avg_loss_r = (sum(r.r_multiple for r in losses) / len(losses)) if losses else 0.0
    return GroupStats(
        key=key, count=n, wins=len(wins), losses=len(losses),
        win_rate=len(wins) / n, expectancy_r=expectancy_r,
        avg_win_r=avg_win_r, avg_loss_r=avg_loss_r,
        total_pnl=total_pnl, avg_pnl=total_pnl / n,
    )


class TradingAnalytics:
    def __init__(self, records: List[TradeRecordV4]) -> None:
        self.records = records

    def overall(self) -> GroupStats:
        return _stats('overall', self.records)

    def group_by(self, key_func: Callable[[TradeRecordV4], str]) -> Dict[str, GroupStats]:
        buckets: Dict[str, List[TradeRecordV4]] = {}
        for r in self.records:
            buckets.setdefault(str(key_func(r)), []).append(r)
        return {k: _stats(k, v) for k, v in buckets.items()}

    def by_symbol(self) -> Dict[str, GroupStats]:
        return self.group_by(lambda r: r.symbol)

    def by_session(self) -> Dict[str, GroupStats]:
        return self.group_by(lambda r: r.session)

    def by_engine(self) -> Dict[str, GroupStats]:
        return self.group_by(lambda r: r.engine)

    def by_regime(self) -> Dict[str, GroupStats]:
        return self.group_by(lambda r: r.regime)

    def by_side(self) -> Dict[str, GroupStats]:
        return self.group_by(lambda r: r.side)

    def by_confluence(self) -> Dict[str, GroupStats]:
        return self.group_by(lambda r: str(r.confluence))
