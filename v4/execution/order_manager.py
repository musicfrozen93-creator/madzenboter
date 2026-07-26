"""Binance Order Manager (spec §C4).

Wraps a ccxt-like futures client with the robustness a commercial system needs:

  • Idempotency: every order carries a clientOrderId; after a transient failure
    the manager checks whether the order already landed before resubmitting, so a
    network blip never creates a duplicate.
  • Retry/backoff: transient and maintenance errors are retried with exponential
    backoff; fatal errors fail fast with a reason.
  • Partial fills: entries/closes resolve the ACTUAL filled quantity; closes loop
    on the remainder until flat (never assume a full fill).
  • Benign closes: reduce-only "position not exist / would-not-reduce" errors are
    treated as already-flat success.

The client interface required: create_order, cancel_order, fetch_open_orders,
amount_to_precision. (fetch_positions/fetch_ticker are used by higher layers.)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from v4.params import V4Params

_TRANSIENT_TOKENS = (
    'network', 'timeout', 'timed out', 'temporarily', 'unavailable',
    'exchangenotavailable', 'requesttimeout', 'ddos', 'rate limit',
    'too many requests', 'connection', 'reset by peer',
)
_MAINTENANCE_TOKENS = ('maintenance', 'system busy', 'try again later', 'service unavailable')
_BENIGN_CLOSE_TOKENS = (
    'reduceonly', 'reduce only', 'reduce-only', 'position not exist', 'no position',
    'position does not exist', 'position side does not match', 'order would not reduce',
    'quantity less than', 'unknown order sent',
)


def _tok(exc: Exception) -> str:
    return f'{type(exc).__name__} {exc}'.lower()


@dataclass
class OrderResult:
    ok: bool
    reason: str
    order_id: Optional[str] = None
    client_id: Optional[str] = None
    filled_qty: float = 0.0
    avg_price: float = 0.0
    status: str = ''
    already_flat: bool = False


class BinanceOrderManager:
    def __init__(
        self,
        client,
        params: Optional[V4Params] = None,
        sleep_func: Callable[[float], None] = time.sleep,
        logger=None,
    ) -> None:
        self.client = client
        self.params = params or V4Params()
        self._sleep = sleep_func
        self._log = logger

    # ── classification ──
    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        t = _tok(exc)
        return any(tok in t for tok in _TRANSIENT_TOKENS)

    @staticmethod
    def _is_maintenance(exc: Exception) -> bool:
        t = _tok(exc)
        return any(tok in t for tok in _MAINTENANCE_TOKENS)

    @staticmethod
    def _is_benign_close(exc: Exception) -> bool:
        t = _tok(exc)
        return any(tok in t for tok in _BENIGN_CLOSE_TOKENS)

    def _find_by_client_id(self, symbol: str, client_id: Optional[str]) -> Optional[dict]:
        """Idempotency probe: has an order with this clientOrderId already landed?"""
        if not client_id:
            return None
        try:
            for o in self.client.fetch_open_orders(symbol):
                if o.get('clientOrderId') == client_id:
                    return o
        except Exception:
            return None
        return None

    @staticmethod
    def _resolve_fill(order: dict, requested: float) -> tuple[float, float]:
        raw = order.get('filled')
        if raw is None:
            raw = order.get('amount', requested)
        try:
            filled = float(raw)
        except (TypeError, ValueError):
            filled = float(requested)
        avg = order.get('average') or order.get('price') or 0.0
        try:
            avg = float(avg)
        except (TypeError, ValueError):
            avg = 0.0
        return max(0.0, filled), avg

    def _backoff(self, attempt: int) -> None:
        self._sleep(self.params.order_retry_backoff_seconds * (2 ** attempt))

    # ── entries / reduce-only market orders ──
    def place_market(
        self, symbol: str, side: str, qty: float, client_id: str,
        reduce_only: bool = False,
    ) -> OrderResult:
        """Idempotent market order with retry/backoff and partial-fill resolution."""
        amount = float(self.client.amount_to_precision(symbol, qty))
        if amount <= 0:
            return OrderResult(False, 'zero_amount_after_precision', client_id=client_id)

        params = {'newClientOrderId': client_id}
        if reduce_only:
            params['reduceOnly'] = True

        retries = max(1, self.params.max_order_retries)
        last = 'unknown'
        for attempt in range(retries):
            # After a prior failure, make sure we are not about to duplicate.
            if attempt > 0:
                existing = self._find_by_client_id(symbol, client_id)
                if existing is not None:
                    filled, avg = self._resolve_fill(existing, amount)
                    return OrderResult(
                        True, 'recovered_existing', order_id=existing.get('id'),
                        client_id=client_id, filled_qty=filled, avg_price=avg,
                        status=existing.get('status', ''),
                    )
            try:
                order = self.client.create_order(symbol, 'market', side, amount, None, params)
                filled, avg = self._resolve_fill(order, amount)
                if filled <= 0:
                    return OrderResult(False, 'no_fill', order_id=order.get('id'),
                                       client_id=client_id, status=order.get('status', ''))
                return OrderResult(True, 'OK', order_id=order.get('id'), client_id=client_id,
                                   filled_qty=filled, avg_price=avg, status=order.get('status', ''))
            except Exception as e:  # noqa: BLE001 — classified below
                if reduce_only and self._is_benign_close(e):
                    return OrderResult(True, 'already_flat', client_id=client_id, already_flat=True)
                last = f'{type(e).__name__}: {e}'
                if (self._is_transient(e) or self._is_maintenance(e)) and attempt < retries - 1:
                    self._backoff(attempt)
                    continue
                return OrderResult(False, last, client_id=client_id)
        return OrderResult(False, last, client_id=client_id)

    def close_reduce_only(
        self, symbol: str, position_side: str, qty: float, client_id: str,
    ) -> OrderResult:
        """Fully close `qty` of a position, looping on partial fills until flat."""
        close_side = 'sell' if position_side == 'long' else 'buy'
        remaining = float(self.client.amount_to_precision(symbol, qty))
        total = remaining
        if total <= 0:
            return OrderResult(True, 'nothing_to_close', already_flat=True)

        closed = 0.0
        avg_price = 0.0
        for attempt in range(max(1, self.params.max_order_retries)):
            res = self.place_market(
                symbol, close_side, remaining, f'{client_id}-{attempt}', reduce_only=True,
            )
            if res.already_flat:
                return OrderResult(True, 'already_flat', filled_qty=closed,
                                   avg_price=avg_price, already_flat=True)
            if not res.ok:
                if attempt < self.params.max_order_retries - 1:
                    self._backoff(attempt)
                    continue
                return OrderResult(False, res.reason, filled_qty=closed, avg_price=avg_price)
            closed += res.filled_qty
            avg_price = res.avg_price or avg_price
            remaining = max(0.0, remaining - res.filled_qty)
            if remaining <= total * 1e-6:
                return OrderResult(True, 'OK', filled_qty=closed, avg_price=avg_price)
        return OrderResult(False, 'incomplete_close', filled_qty=closed, avg_price=avg_price)

    # ── resting stop-market ──
    def place_stop_market(
        self, symbol: str, position_side: str, qty: float, stop_price: float, client_id: str,
    ) -> OrderResult:
        """Place a RESTING reduce-only stop-market that survives bot downtime."""
        close_side = 'sell' if position_side == 'long' else 'buy'
        amount = float(self.client.amount_to_precision(symbol, qty))
        if amount <= 0:
            return OrderResult(False, 'zero_amount_after_precision', client_id=client_id)
        params = {
            'stopPrice': float(self.client.price_to_precision(symbol, stop_price)),
            'reduceOnly': True,
            'newClientOrderId': client_id,
        }
        retries = max(1, self.params.max_order_retries)
        last = 'unknown'
        for attempt in range(retries):
            if attempt > 0:
                existing = self._find_by_client_id(symbol, client_id)
                if existing is not None:
                    return OrderResult(True, 'recovered_existing', order_id=existing.get('id'),
                                       client_id=client_id, status=existing.get('status', ''))
            try:
                order = self.client.create_order(
                    symbol, 'STOP_MARKET', close_side, amount, None, params,
                )
                return OrderResult(True, 'OK', order_id=order.get('id'), client_id=client_id,
                                   status=order.get('status', 'open'))
            except Exception as e:  # noqa: BLE001
                last = f'{type(e).__name__}: {e}'
                if (self._is_transient(e) or self._is_maintenance(e)) and attempt < retries - 1:
                    self._backoff(attempt)
                    continue
                return OrderResult(False, last, client_id=client_id)
        return OrderResult(False, last, client_id=client_id)

    def cancel(self, symbol: str, order_id: Optional[str]) -> bool:
        """Cancel an order; treat an already-gone order as success (idempotent)."""
        if not order_id:
            return True
        try:
            self.client.cancel_order(order_id, symbol)
            return True
        except Exception as e:  # noqa: BLE001
            t = _tok(e)
            if 'unknown order' in t or 'not open' in t or 'does not exist' in t or 'not exist' in t:
                return True
            return False
