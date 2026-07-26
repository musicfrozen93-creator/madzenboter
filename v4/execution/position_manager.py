"""V4 Position Manager (spec §C1, §C2, §C4).

Owns the full lifecycle of ONE position:
  • open       — place the market entry, resolve the ACTUAL fill, build the
                 TradeState from the filled quantity, and attach a RESTING
                 exchange stop at the structural stop price.
  • manage     — advance the trade_math state machine one tick and execute the
                 resulting actions (partial reduce-only close, cancel+replace the
                 resting stop for break-even/trailing, or full close).
  • reconcile  — if the exchange shows no live position, finalize and cancel any
                 stray resting stop (external close / liquidation / lost fill).

Nothing here recomputes strategy; it executes the validated plan produced by the
sizer and the deterministic trade_math state machine.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from v4.execution.exchange_stops import ExchangeStopManager
from v4.execution.order_manager import BinanceOrderManager
from v4.params import V4Params
from v4.sizing import SizingPlan
from v4.trade_math import TradeState, manage as manage_state


@dataclass
class V4Position:
    """Live V4 position with its serializable trade-management state."""

    symbol: str
    side: str
    state: TradeState
    leverage: int
    entry_order_id: Optional[str] = None
    stop_order_id: Optional[str] = None
    account_id: Optional[int] = None
    opened_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    closed: bool = False

    @property
    def open_risk_usd(self) -> float:
        """Remaining risk to the current stop (0 once at break-even/profit trail)."""
        st = self.state
        per_unit = abs(st.entry - st.current_stop)
        return per_unit * st.remaining_qty


@dataclass
class ManageOutcome:
    actions: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class V4PositionManager:
    def __init__(
        self,
        order_manager: BinanceOrderManager,
        stop_manager: ExchangeStopManager,
        params: Optional[V4Params] = None,
    ) -> None:
        self.om = order_manager
        self.stops = stop_manager
        self.params = params or V4Params()

    def open(
        self, plan: SizingPlan, account_id: Optional[int] = None,
        strong_trend: bool = False,
    ) -> Optional[V4Position]:
        """Open a position from a SUITABLE sizing plan; None if it cannot fill."""
        if not plan.suitable:
            return None
        if not plan.symbol:
            raise ValueError('SizingPlan.symbol must be set before opening a position')
        symbol_side = plan.side
        pos_id = uuid.uuid4().hex
        entry = self.om.place_market(
            symbol=plan.symbol, side=('buy' if symbol_side == 'long' else 'sell'),
            qty=plan.quantity, client_id=f'v4-{pos_id}-entry',
        )
        if not entry.ok or entry.filled_qty <= 0:
            return None

        fill_price = entry.avg_price or plan.entry
        state = TradeState.open(
            side=symbol_side, entry=fill_price, stop_price=plan.stop_price,
            quantity=entry.filled_qty, params=self.params, strong_trend=strong_trend,
        )
        pos = V4Position(
            symbol=plan.symbol, side=symbol_side, state=state, leverage=plan.leverage,
            entry_order_id=entry.order_id, account_id=account_id, id=pos_id,
        )
        # Attach the resting exchange stop for the ACTUAL filled quantity.
        stop_res = self.stops.place(
            plan.symbol, symbol_side, entry.filled_qty, plan.stop_price,
            client_id=f'v4-{pos_id}-stop0',
        )
        if stop_res.ok:
            pos.stop_order_id = stop_res.order_id
        return pos

    # (symbol is carried on the SizingPlan; no helper indirection needed)

    def manage(self, pos: V4Position, price: float, atr: float) -> ManageOutcome:
        """Advance one position by one price tick and execute the actions."""
        out = ManageOutcome()
        if pos.closed or pos.state.closed:
            return out

        actions = manage_state(pos.state, price, atr, self.params)
        for a in actions:
            if a.type == 'take_partial':
                res = self.om.close_reduce_only(
                    pos.symbol, pos.side, a.quantity, client_id=f'v4-{pos.id}-partial',
                )
                out.actions.append(f'partial:{res.reason}')
                if not res.ok and not res.already_flat:
                    out.errors.append(f'partial_failed:{res.reason}')
            elif a.type == 'move_stop':
                stamp = str(int(a.stop * 1e6))
                res = self.stops.move(
                    pos.symbol, pos.side, pos.state.remaining_qty, a.stop,
                    new_client_id=f'v4-{pos.id}-stop-{a.reason}-{stamp}',
                    current_stop_order_id=pos.stop_order_id,
                )
                if res.ok:
                    pos.stop_order_id = res.order_id
                    out.actions.append(f'move_stop:{a.reason}')
                else:
                    out.errors.append(f'move_stop_failed:{res.reason}')
            elif a.type == 'close':
                res = self.om.close_reduce_only(
                    pos.symbol, pos.side, a.quantity, client_id=f'v4-{pos.id}-close',
                )
                self.stops.cancel(pos.symbol, pos.stop_order_id)
                pos.stop_order_id = None
                pos.closed = True
                out.actions.append(f'close:{a.reason}:{res.reason}')
                if not res.ok and not res.already_flat:
                    out.errors.append(f'close_failed:{res.reason}')
        return out

    def force_close(self, pos: V4Position, reason: str) -> ManageOutcome:
        """Flatten a position now (circuit breaker / kill switch) and cancel its stop."""
        out = ManageOutcome()
        if pos.closed:
            return out
        res = self.om.close_reduce_only(
            pos.symbol, pos.side, pos.state.remaining_qty, client_id=f'v4-{pos.id}-force',
        )
        self.stops.cancel(pos.symbol, pos.stop_order_id)
        pos.stop_order_id = None
        pos.state.remaining_qty = 0.0
        pos.state.closed = True
        pos.closed = True
        out.actions.append(f'force_close:{reason}:{res.reason}')
        if not res.ok and not res.already_flat:
            out.errors.append(f'force_close_failed:{res.reason}')
        return out

    def reconcile(self, pos: V4Position, live_keys: set, grace_seconds: float = 60.0) -> bool:
        """Finalize a position whose exchange leg has vanished. Returns True if closed."""
        if pos.closed:
            return True
        if time.time() - pos.opened_at < grace_seconds:
            return False
        if (pos.symbol, pos.side) in live_keys:
            return False
        # No live exchange position → finalize and cancel any stray resting stop.
        self.stops.cancel(pos.symbol, pos.stop_order_id)
        pos.stop_order_id = None
        pos.state.remaining_qty = 0.0
        pos.state.closed = True
        pos.closed = True
        return True
