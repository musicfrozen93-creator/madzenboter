"""ZenGrid — Admin API Trade Routes."""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from admin.dependencies import get_database, verify_api_key
from admin.schemas import TradeResponse, TradeSummary
from core.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.get('/', response_model=List[TradeResponse])
async def list_trades(
    account_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_database),
):
    """List trades with pagination."""
    trades = db.get_all_trades(
        account_id=account_id, limit=limit, offset=offset
    )
    return [
        TradeResponse(
            id=t.id,
            account_id=t.account_id,
            basket_id=t.basket_id,
            symbol=t.symbol,
            side=t.side,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            quantity=t.quantity,
            margin=t.margin,
            leverage=t.leverage,
            pnl=t.pnl,
            fee=t.fee,
            layers_used=t.layers_used,
            entry_time=t.entry_time,
            exit_time=t.exit_time,
            exit_reason=t.exit_reason,
        )
        for t in trades
    ]


@router.get('/summary', response_model=TradeSummary)
async def trade_summary(
    account_id: Optional[int] = Query(None),
    db: Database = Depends(get_database),
):
    """Get aggregate trade summary."""
    trades = db.get_all_trades(account_id=account_id, limit=10000)
    total = len(trades)
    if total == 0:
        return TradeSummary(
            total_trades=0, total_pnl=0.0, total_fees=0.0,
            winning_trades=0, losing_trades=0, win_rate=0.0, avg_pnl=0.0,
        )
    total_pnl = sum(t.pnl for t in trades)
    total_fees = sum(t.fee for t in trades)
    winning = sum(1 for t in trades if t.pnl > 0)
    losing = sum(1 for t in trades if t.pnl <= 0)
    return TradeSummary(
        total_trades=total,
        total_pnl=round(total_pnl, 4),
        total_fees=round(total_fees, 4),
        winning_trades=winning,
        losing_trades=losing,
        win_rate=round(winning / total, 4) if total > 0 else 0.0,
        avg_pnl=round(total_pnl / total, 4) if total > 0 else 0.0,
    )
