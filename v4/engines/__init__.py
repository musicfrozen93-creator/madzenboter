"""V4 signal engines (spec §3, §4).

One engine per regime, selected deterministically by the Regime Engine's router:
  • TrendEngine    — pullback-continuation inside an established trend
  • BreakoutEngine — squeeze-release / structure break with confirmation

Each engine emits a V4Signal only when its mandatory regime precondition holds
AND the confluence count clears the launch gate (spec §A5: confluence count, not
an uncalibrated 0–100 score). Range/mean-reversion is a later-phase sleeve.
"""

from v4.engines.base import V4Signal, score_confluence
from v4.engines.breakout import BreakoutEngine
from v4.engines.trend import TrendEngine

__all__ = ['V4Signal', 'score_confluence', 'TrendEngine', 'BreakoutEngine']
