"""ZenGrid — Admin API Bot Control Routes.

Endpoints for starting/stopping the bot, managing position monitoring,
emergency stop, force-closing all positions, and querying live bot status.

Route prefix: /api/admin/bot
All routes require X-API-Key authentication.
"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, status

from admin.dependencies import (
    get_bot_control,
    get_database,
    get_signal_executor,
    verify_api_key,
)
from control.bot_control import BotControl
from core.database import Database

logger = logging.getLogger(__name__)
control_logger = logging.getLogger('zentry.control')

router = APIRouter(
    prefix='/bot',
    dependencies=[Depends(verify_api_key)],
    tags=['Bot Control'],
)


# ─────────────────────────────────────────────
# Start / Stop
# ─────────────────────────────────────────────

@router.post('/start')
async def bot_start(
    bot_control: BotControl = Depends(get_bot_control),
):
    """Enable the bot: scanner runs, new trades can open.

    Also clears any active emergency_stop so the bot resumes fully.
    """
    bot_control.start_bot()
    snap = bot_control.snapshot()
    return {
        'message': 'Bot started',
        'bot_enabled': snap.bot_enabled,
        'emergency_stop': snap.emergency_stop,
        'timestamp': time.time(),
    }


@router.post('/stop')
async def bot_stop(
    bot_control: BotControl = Depends(get_bot_control),
):
    """Disable the bot: scanner stops, no new trades.

    Existing positions continue to be managed (TP/SL/trailing) unless
    position management is also disabled.
    """
    bot_control.stop_bot()
    snap = bot_control.snapshot()
    return {
        'message': 'Bot stopped — existing positions still managed',
        'bot_enabled': snap.bot_enabled,
        'manage_existing_positions': snap.manage_existing_positions,
        'timestamp': time.time(),
    }


# ─────────────────────────────────────────────
# Emergency Stop
# ─────────────────────────────────────────────

@router.post('/emergency-stop')
async def bot_emergency_stop(
    bot_control: BotControl = Depends(get_bot_control),
    database: Database = Depends(get_database),
):
    """Activate EMERGENCY_STOP mode.

    Immediately halts:
      • Scanner
      • Signal execution
      • New position opening
      • Recovery layer placement
      • Cancels all pending (open) orders across all accounts

    Does NOT close existing positions — TP/SL/trailing/risk management
    remain fully active. Use /close-all to force-close all positions.
    """
    bot_control.set_emergency_stop()
    control_logger.info('[CONTROL] Emergency Stop requested via Admin API')

    cancel_summary = {'accounts_processed': 0, 'orders_cancelled': 0, 'orders_failed': 0}
    executor = get_signal_executor()
    if executor is not None:
        try:
            cancel_summary = executor.cancel_all_pending_orders(database)
        except Exception as e:
            logger.error('Failed to cancel pending orders during emergency stop: %s', e)
            control_logger.error(
                '[CONTROL] Pending order cancellation failed: %s', e
            )

    snap = bot_control.snapshot()
    return {
        'message': 'Emergency stop activated — scanner, signals and new orders halted. '
                   'Existing positions remain protected (TP/SL active).',
        'bot_enabled': snap.bot_enabled,
        'emergency_stop': snap.emergency_stop,
        'pending_orders_cancelled': cancel_summary.get('orders_cancelled', 0),
        'pending_orders_failed': cancel_summary.get('orders_failed', 0),
        'timestamp': time.time(),
    }


@router.post('/clear-emergency-stop')
async def bot_clear_emergency_stop(
    bot_control: BotControl = Depends(get_bot_control),
):
    """Deactivate EMERGENCY_STOP and resume normal bot operation."""
    bot_control.clear_emergency_stop()
    snap = bot_control.snapshot()
    return {
        'message': 'Emergency stop cleared — bot resumed',
        'bot_enabled': snap.bot_enabled,
        'emergency_stop': snap.emergency_stop,
        'timestamp': time.time(),
    }


# ─────────────────────────────────────────────
# Force Close All
# ─────────────────────────────────────────────

@router.post('/close-all')
async def bot_close_all(
    bot_control: BotControl = Depends(get_bot_control),
    database: Database = Depends(get_database),
):
    """Safely close ALL open positions across ALL accounts.

    This is a synchronous, blocking operation that:
    1. Disables new trades.
    2. Closes each basket one by one.
    3. Logs every action.
    4. Returns a summary report.
    """
    executor = get_signal_executor()
    if executor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Signal executor not available (bot may not have account trading enabled)',
        )

    summary = bot_control.request_force_close_all(executor, database)

    if 'error' in summary:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=summary['error'],
        )

    return {
        'message': 'Force close completed',
        'summary': summary,
        'bot_enabled': bot_control.bot_enabled,
        'timestamp': time.time(),
    }


# ─────────────────────────────────────────────
# Position Management Toggles
# ─────────────────────────────────────────────

@router.post('/manage-positions/enable')
async def enable_position_management(
    bot_control: BotControl = Depends(get_bot_control),
):
    """Enable active management (TP/SL/recovery) of existing positions."""
    bot_control.enable_position_management()
    return {
        'message': 'Position management enabled',
        'manage_existing_positions': True,
        'timestamp': time.time(),
    }


@router.post('/manage-positions/disable')
async def disable_position_management(
    bot_control: BotControl = Depends(get_bot_control),
):
    """Disable active management — monitoring only, no close/recovery orders."""
    bot_control.disable_position_management()
    return {
        'message': 'Position management disabled (monitoring only)',
        'manage_existing_positions': False,
        'timestamp': time.time(),
    }


# ─────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────

@router.get('/status')
async def bot_status(
    bot_control: BotControl = Depends(get_bot_control),
    database: Database = Depends(get_database),
):
    """Full bot status including control state and position counts.

    Response:
        {
          "bot_enabled": true,
          "scanner_running": true,
          "open_positions": 12,
          "open_baskets": 4,
          "manage_existing_positions": true,
          "force_close_all": false,
          "emergency_stop": false,
          "running_strategies": 4,
          "last_action": "Bot Started",
          "last_action_at": 1718639842.5
        }
    """
    snap = bot_control.snapshot()

    open_baskets = database.load_active_baskets()
    open_positions = sum(b.layer_count for b in open_baskets)

    account_ids = {b.account_id for b in open_baskets if b.account_id}
    running_strategies = len(account_ids)

    return {
        'bot_enabled': snap.bot_enabled,
        'scanner_running': snap.scanner_running,
        'open_positions': open_positions,
        'open_baskets': len(open_baskets),
        'manage_existing_positions': snap.manage_existing_positions,
        'force_close_all': snap.force_close_all,
        'emergency_stop': snap.emergency_stop,
        'running_strategies': running_strategies,
        'last_action': snap.last_action,
        'last_action_at': snap.last_action_at,
    }
