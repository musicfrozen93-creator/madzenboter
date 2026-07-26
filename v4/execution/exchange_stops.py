"""Exchange Stop Orders (spec §A7, §C4).

Manages a single RESTING reduce-only stop-market order per position — the fix for
V1's software-only stop. The stop lives on the exchange so a bot/VPS/API outage
cannot leave a position unprotected. Moving the stop (break-even / trailing) is a
cancel-then-replace; a failed replace never leaves the position without a stop
because the old stop is only cancelled after the new one is confirmed placed.
"""

from __future__ import annotations

from typing import Optional

from v4.execution.order_manager import BinanceOrderManager, OrderResult


class ExchangeStopManager:
    def __init__(self, order_manager: BinanceOrderManager) -> None:
        self.om = order_manager

    def place(
        self, symbol: str, position_side: str, qty: float, stop_price: float, client_id: str,
    ) -> OrderResult:
        """Place the initial resting stop for a freshly opened position."""
        return self.om.place_stop_market(symbol, position_side, qty, stop_price, client_id)

    def move(
        self,
        symbol: str,
        position_side: str,
        qty: float,
        new_stop_price: float,
        new_client_id: str,
        current_stop_order_id: Optional[str],
    ) -> OrderResult:
        """Replace the resting stop (break-even / trailing).

        Places the NEW stop first; only if that succeeds is the OLD stop cancelled.
        This guarantees the position is never momentarily stop-less. On failure the
        old stop is left in place and the caller keeps its existing stop id.
        """
        placed = self.om.place_stop_market(symbol, position_side, qty, new_stop_price, new_client_id)
        if not placed.ok:
            return placed
        # New stop confirmed → retire the old one (idempotent).
        self.om.cancel(symbol, current_stop_order_id)
        return placed

    def cancel(self, symbol: str, stop_order_id: Optional[str]) -> bool:
        """Cancel the resting stop (position fully closed)."""
        return self.om.cancel(symbol, stop_order_id)
