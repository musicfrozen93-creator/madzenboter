"""V4 execution layer (spec §C4).

Talks to the exchange with production-grade robustness: idempotent orders
(clientOrderId), retry/backoff on transient/maintenance errors, partial-fill
resolution, benign already-flat handling, and RESTING exchange stop-market orders
so protection survives bot/VPS downtime (the fix for V1's software-only stop).

  • order_manager    — BinanceOrderManager (entries, reduce-only closes, stops)
  • exchange_stops   — ExchangeStopManager (resting stop create/move/cancel)
  • position_manager — V4PositionManager (open / manage / reconcile one position)
  • trade_manager    — V4TradeManager (manage all positions for one account/tick)

NOTE: verified against a fake exchange only (no live keys in this workspace).
"""

from v4.execution.order_manager import BinanceOrderManager, OrderResult
from v4.execution.exchange_stops import ExchangeStopManager
from v4.execution.position_manager import V4Position, V4PositionManager
from v4.execution.trade_manager import V4TradeManager

__all__ = [
    'BinanceOrderManager', 'OrderResult', 'ExchangeStopManager',
    'V4Position', 'V4PositionManager', 'V4TradeManager',
]
