"""Performance Logger (spec §12).

Records each completed trade with the dimensions needed for learning: engine,
regime, session, confluence bucket, exit reason, PnL and R-multiple. The sink is
injectable (a list in tests; a DB writer in production), so this module has no
persistence dependency of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional


def session_of(dt: datetime) -> str:
    """Coarse trading session from a UTC timestamp (spec §4 session filter)."""
    h = dt.astimezone(timezone.utc).hour
    if 0 <= h < 8:
        return 'asia'
    if 8 <= h < 16:
        return 'europe'
    return 'us'


@dataclass(frozen=True)
class TradeRecordV4:
    """Immutable record of one completed V4 trade."""

    symbol: str
    side: str
    engine: str
    regime: str
    session: str
    confluence: int
    pnl: float
    r_multiple: float
    exit_reason: str
    opened_at: float
    closed_at: float
    account_id: Optional[int] = None


class PerformanceLogger:
    def __init__(self, sink: Optional[List[TradeRecordV4]] = None) -> None:
        self.records: List[TradeRecordV4] = sink if sink is not None else []

    def record(
        self,
        symbol: str,
        side: str,
        engine: str,
        regime: str,
        confluence: int,
        pnl: float,
        risk_usd: float,
        exit_reason: str,
        opened_at: float,
        closed_at: float,
        account_id: Optional[int] = None,
        session: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> TradeRecordV4:
        """Record a completed trade; R-multiple = pnl / risk_usd (0 if risk 0)."""
        r_multiple = (pnl / risk_usd) if risk_usd > 0 else 0.0
        if session is None:
            when = now or datetime.fromtimestamp(closed_at, tz=timezone.utc)
            session = session_of(when)
        rec = TradeRecordV4(
            symbol=symbol, side=side, engine=engine, regime=regime, session=session,
            confluence=confluence, pnl=pnl, r_multiple=r_multiple,
            exit_reason=exit_reason, opened_at=opened_at, closed_at=closed_at,
            account_id=account_id,
        )
        self.records.append(rec)
        return rec
