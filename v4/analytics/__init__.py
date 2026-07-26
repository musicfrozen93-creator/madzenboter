"""V4 analytics (spec §12).

  • logger    — PerformanceLogger records every completed trade with the metadata
                needed to learn (engine, regime, session, confluence, R-multiple).
  • analytics — TradingAnalytics computes expectancy/win-rate/PnL grouped by any
                dimension (symbol, session, regime, engine, confluence, side).
  • learning  — PerformanceLearning turns those stats into GATED recommendations
                (prune chronic losers, calibrate confluence) — never auto-applied;
                minimum-sample gated and flagged for human review (spec §12).
"""

from v4.analytics.analytics import GroupStats, TradingAnalytics
from v4.analytics.learning import LearningReport, PerformanceLearning
from v4.analytics.logger import PerformanceLogger, TradeRecordV4, session_of

__all__ = [
    'PerformanceLogger', 'TradeRecordV4', 'session_of',
    'TradingAnalytics', 'GroupStats',
    'PerformanceLearning', 'LearningReport',
]
