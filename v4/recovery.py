"""Restart Recovery + DB↔exchange synchronization (spec §C4).

On startup (or after a crash) the persisted V4 positions must be reconciled with
what the exchange actually holds:

  • Live leg present   → rebuild the in-memory V4Position/TradeState AND ensure a
    RESTING stop exists; if the stop is missing (cancelled during downtime), it is
    re-placed at the persisted current stop so the position is never unprotected.
  • Live leg absent    → the position closed while we were down (stop hit /
    external close / liquidation); finalize it and cancel any stray resting stop.

Pure orchestration over the order/stop managers — fake-verifiable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from v4.execution.exchange_stops import ExchangeStopManager
from v4.execution.order_manager import BinanceOrderManager
from v4.execution.position_manager import V4Position
from v4.trade_math import TradeState


@dataclass
class PersistedPosition:
    """Flat, serializable snapshot of a V4 position (as stored in the DB)."""

    id: str
    symbol: str
    side: str
    entry: float
    initial_stop: float
    current_stop: float
    quantity: float
    remaining_qty: float
    partial_price: float
    r_value: float
    leverage: int
    partial_done: bool = False
    breakeven_set: bool = False
    strong_trend: bool = False
    stop_order_id: Optional[str] = None
    account_id: Optional[int] = None
    opened_at: float = 0.0


@dataclass
class RecoveryReport:
    rebuilt: List[V4Position] = field(default_factory=list)
    finalized: List[PersistedPosition] = field(default_factory=list)
    stops_replaced: int = 0
    errors: List[str] = field(default_factory=list)


class RecoveryManager:
    def __init__(
        self, order_manager: BinanceOrderManager, stop_manager: ExchangeStopManager,
    ) -> None:
        self.om = order_manager
        self.stops = stop_manager

    def recover(
        self,
        persisted: List[PersistedPosition],
        live_positions: List[dict],
        symbols_with_open_stop: Optional[set] = None,
    ) -> RecoveryReport:
        """Reconcile persisted positions with live exchange positions on restart."""
        report = RecoveryReport()
        live_keys = {
            (p.get('symbol'), (p.get('side') or '').lower())
            for p in live_positions
            if float(p.get('contracts', 0) or 0) > 0
        }
        with_stop = symbols_with_open_stop if symbols_with_open_stop is not None else set()

        for rec in persisted:
            key = (rec.symbol, rec.side)
            if key not in live_keys:
                # Position gone → finalize + cancel any stray stop.
                self.stops.cancel(rec.symbol, rec.stop_order_id)
                report.finalized.append(rec)
                continue

            state = TradeState(
                side=rec.side, entry=rec.entry, initial_stop=rec.initial_stop,
                current_stop=rec.current_stop, quantity=rec.quantity,
                remaining_qty=rec.remaining_qty, partial_price=rec.partial_price,
                r_value=rec.r_value, partial_done=rec.partial_done,
                breakeven_set=rec.breakeven_set, strong_trend=rec.strong_trend,
                closed=False,
            )
            pos = V4Position(
                symbol=rec.symbol, side=rec.side, state=state, leverage=rec.leverage,
                stop_order_id=rec.stop_order_id, account_id=rec.account_id,
                opened_at=rec.opened_at, id=rec.id,
            )

            # Ensure a resting stop exists; re-place if it was lost during downtime.
            if rec.symbol not in with_stop:
                res = self.stops.place(
                    rec.symbol, rec.side, rec.remaining_qty, rec.current_stop,
                    client_id=f'v4-{rec.id}-stop-recover',
                )
                if res.ok:
                    pos.stop_order_id = res.order_id
                    report.stops_replaced += 1
                else:
                    report.errors.append(f'restop_failed:{rec.symbol}:{res.reason}')

            report.rebuilt.append(pos)

        return report
