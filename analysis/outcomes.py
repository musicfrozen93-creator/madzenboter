"""Signal Outcome Tracker (Phase 1 F7).

Persists every actionable signal with its original snapshot and tracks its
lifecycle: OPEN → TP1_HIT / TP2_HIT / TP3_HIT / SL_HIT / EXPIRED / INVALIDATED.

The tracker never uses future data — it stores the snapshot at signal time and
only compares against price data that is strictly after the signal timestamp.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from analysis.generator import TradingSignal
from analysis.scoring import Score
from core.models import SignalOutcomeModel

logger = logging.getLogger(__name__)

OPEN = 'OPEN'
TP1_HIT = 'TP1_HIT'
TP2_HIT = 'TP2_HIT'
TP3_HIT = 'TP3_HIT'
SL_HIT = 'SL_HIT'
EXPIRED = 'EXPIRED'
INVALIDATED = 'INVALIDATED'

TERMINAL_STATUSES = frozenset({TP3_HIT, SL_HIT, EXPIRED, INVALIDATED})
DEFAULT_EXPIRY_HOURS = 48


@dataclass(frozen=True)
class OutcomeSnapshot:
    """Immutable record of the signal at the moment it was emitted."""

    direction: str
    entry: float
    stop_loss: float
    take_profits: List[float]
    risk_reward: float
    rr_per_tp: List[float]
    quality: int
    confidence: int
    entry_basis: Optional[str]
    stop_basis: Optional[str]
    target_sources: List[str]

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(',', ':'))


def record_signal(
    db: Session,
    signal: TradingSignal,
    quality: Score,
    confidence: Score,
) -> Optional[SignalOutcomeModel]:
    """Persist an actionable signal for outcome tracking. Returns None for WAIT."""
    if not signal.actionable:
        return None

    tps = signal.take_profits
    snapshot = OutcomeSnapshot(
        direction=signal.direction,
        entry=signal.entry,
        stop_loss=signal.stop_loss,
        take_profits=tps,
        risk_reward=signal.risk_reward,
        rr_per_tp=signal.rr_per_tp,
        quality=quality.value,
        confidence=confidence.value,
        entry_basis=signal.entry_basis,
        stop_basis=signal.stop_basis,
        target_sources=signal.target_sources,
    )

    row = SignalOutcomeModel(
        symbol=signal.symbol,
        timeframe=signal.timeframe,
        market=signal.market,
        provider=signal.provider,
        direction=signal.direction,
        entry=signal.entry,
        stop_loss=signal.stop_loss,
        tp1=tps[0] if len(tps) > 0 else 0.0,
        tp2=tps[1] if len(tps) > 1 else 0.0,
        tp3=tps[2] if len(tps) > 2 else 0.0,
        risk_reward=signal.risk_reward,
        quality=quality.value,
        confidence=confidence.value,
        snapshot=snapshot.to_json(),
        status=OPEN,
    )
    db.add(row)
    db.flush()
    logger.info('OUTCOME | recorded %s %s %s id=%d', signal.direction, signal.symbol, signal.timeframe, row.id)
    return row


def update_outcomes(
    db: Session,
    symbol: str,
    current_price: float,
    now: Optional[datetime] = None,
    expiry_hours: int = DEFAULT_EXPIRY_HOURS,
) -> List[SignalOutcomeModel]:
    """Check open outcomes for the symbol and update their status.

    Only uses the current price — never looks back at candles that formed
    before the signal, preventing look-ahead bias.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=expiry_hours)

    open_outcomes: List[SignalOutcomeModel] = (
        db.query(SignalOutcomeModel)
        .filter(
            SignalOutcomeModel.symbol == symbol,
            SignalOutcomeModel.status.notin_(TERMINAL_STATUSES),
        )
        .all()
    )

    updated = []
    for outcome in open_outcomes:
        new_status = _evaluate(outcome, current_price, cutoff)
        if new_status and new_status != outcome.status:
            outcome.status = new_status
            outcome.close_price = current_price
            outcome.closed_at = now
            outcome.pnl_r = _pnl_in_r(outcome, current_price)
            updated.append(outcome)
            logger.info(
                'OUTCOME | %s %s → %s at %.8f (%.2fR)',
                outcome.symbol, outcome.direction, new_status,
                current_price, outcome.pnl_r or 0.0,
            )

    return updated


def _evaluate(
    outcome: SignalOutcomeModel,
    price: float,
    expiry_cutoff: datetime,
) -> Optional[str]:
    """Determine the new status, or None if unchanged."""
    is_long = outcome.direction == 'BUY'

    if (is_long and price <= outcome.stop_loss) or (not is_long and price >= outcome.stop_loss):
        return SL_HIT

    if outcome.opened_at < expiry_cutoff:
        return EXPIRED

    if is_long:
        if price >= outcome.tp3:
            return TP3_HIT
        if price >= outcome.tp2 and outcome.status in (OPEN, TP1_HIT):
            return TP2_HIT
        if price >= outcome.tp1 and outcome.status == OPEN:
            return TP1_HIT
    else:
        if price <= outcome.tp3:
            return TP3_HIT
        if price <= outcome.tp2 and outcome.status in (OPEN, TP1_HIT):
            return TP2_HIT
        if price <= outcome.tp1 and outcome.status == OPEN:
            return TP1_HIT

    return None


def _pnl_in_r(outcome: SignalOutcomeModel, close_price: float) -> float:
    """P&L expressed in R-multiples."""
    risk = abs(outcome.entry - outcome.stop_loss)
    if risk <= 0:
        return 0.0
    raw = close_price - outcome.entry if outcome.direction == 'BUY' else outcome.entry - close_price
    return round(raw / risk, 2)
