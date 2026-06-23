"""ZenGrid — Admin API Position Routes."""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from admin.dependencies import get_database, verify_api_key
from admin.schemas import PositionResponse
from core.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.get('/', response_model=List[PositionResponse])
async def list_positions(
    account_id: Optional[int] = Query(None),
    status: Optional[str] = Query('open'),
    db: Database = Depends(get_database),
):
    """List positions, optionally filtered by account and status."""
    positions = db.get_positions(account_id=account_id, status=status)
    return [
        PositionResponse(
            id=p.id,
            account_id=p.account_id,
            symbol=p.symbol,
            side=p.side,
            quantity=p.quantity,
            entry_price=p.entry_price,
            unrealized_pnl=p.unrealized_pnl,
            leverage=p.leverage,
            status=p.status,
            opened_at=str(p.opened_at),
        )
        for p in positions
    ]
