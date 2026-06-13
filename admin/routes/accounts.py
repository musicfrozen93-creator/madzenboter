"""ZenGrid — Admin API Account Routes.

Handles all account management endpoints: creation, retrieval, updates,
deletion, enabling/disabling, and live syncing of balance/positions.
"""

import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from accounts.encryption import EncryptionService
from accounts.manager import AccountManager
from admin.dependencies import get_database, get_db_session, verify_api_key
from admin.schemas import (
    AccountCreateRequest,
    AccountResponse,
    AccountUpdateRequest,
    ErrorResponse,
    SuccessResponse,
)
from config.settings import Settings
from core.database import Database
from core.models import AccountModel

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_api_key)])


def _get_encryption_and_settings() -> tuple[EncryptionService, Settings]:
    """Helper to load master encryption service and settings."""
    try:
        settings = Settings.load(os.environ.get('CONFIG_PATH', 'config/config.json'))
    except Exception:
        settings = Settings()

    if not settings.master_encryption_key:
        # Fallback to env var directly
        key = os.environ.get('MASTER_ENCRYPTION_KEY', '')
        settings.master_encryption_key = key

    if not settings.master_encryption_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='MASTER_ENCRYPTION_KEY settings or environment variable is not configured',
        )

    encryption = EncryptionService(settings.master_encryption_key)
    return encryption, settings


def _account_to_response(account: AccountModel, encryption: EncryptionService) -> AccountResponse:
    """Helper to convert AccountModel to AccountResponse with masked API key."""
    try:
        decrypted_key = encryption.decrypt(account.encrypted_api_key)
        masked_api_key = EncryptionService.mask_key(decrypted_key)
    except Exception:
        masked_api_key = '****'

    return AccountResponse(
        id=account.id,
        user_id=account.user_id,
        label=account.label,
        masked_api_key=masked_api_key,
        is_active=account.is_active,
        use_testnet=account.use_testnet,
        risk_pct=account.risk_pct,
        max_positions=account.max_positions,
        leverage_override=account.leverage_override,
        tp_settings=account.tp_settings,
        sl_settings=account.sl_settings,
        cached_balance=account.cached_balance,
        last_sync_at=str(account.last_sync_at) if account.last_sync_at else None,
        created_at=str(account.created_at),
        updated_at=str(account.updated_at),
    )


@router.post('/', response_model=AccountResponse, status_code=status.HTTP_201_CREATED, responses={400: {'model': ErrorResponse}, 500: {'model': ErrorResponse}})
async def create_account(
    req: AccountCreateRequest,
    db: Database = Depends(get_database),
):
    """Create a new trading account and validate credentials with exchange."""
    encryption, settings = _get_encryption_and_settings()
    manager = AccountManager(db, encryption, settings)

    try:
        account = manager.create_account(
            user_id=req.user_id,
            label=req.label,
            api_key=req.api_key,
            api_secret=req.api_secret,
            is_active=req.is_active,
            use_testnet=req.use_testnet,
            risk_pct=req.risk_pct,
            max_positions=req.max_positions,
            leverage_override=req.leverage_override,
            tp_settings=req.tp_settings,
            sl_settings=req.sl_settings,
        )
        return _account_to_response(account, encryption)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to create account: {e}',
        )


@router.get('/', response_model=List[AccountResponse])
async def list_accounts(
    active_only: bool = Query(False),
    session: Session = Depends(get_db_session),
):
    """List all accounts with credentials masked."""
    encryption, _ = _get_encryption_and_settings()
    query = session.query(AccountModel)
    if active_only:
        query = query.filter(AccountModel.is_active.is_(True))

    accounts = query.order_by(AccountModel.id).all()
    return [_account_to_response(acc, encryption) for acc in accounts]


@router.get('/{account_id}', response_model=AccountResponse)
async def get_account(
    account_id: int,
    session: Session = Depends(get_db_session),
):
    """Fetch details of a single account."""
    encryption, _ = _get_encryption_and_settings()
    account = session.get(AccountModel, account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Account with id {account_id} not found',
        )
    return _account_to_response(account, encryption)


@router.put('/{account_id}', response_model=AccountResponse)
async def update_account(
    account_id: int,
    req: AccountUpdateRequest,
    db: Database = Depends(get_database),
):
    """Update account settings or credentials."""
    encryption, settings = _get_encryption_and_settings()
    manager = AccountManager(db, encryption, settings)

    # Convert schema to kwargs
    update_data = req.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No fields provided for update',
        )

    try:
        account = manager.update_account(account_id, **update_data)
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f'Account with id {account_id} not found',
            )
        return _account_to_response(account, encryption)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to update account: {e}',
        )


@router.delete('/{account_id}', response_model=SuccessResponse)
async def delete_account(
    account_id: int,
    hard: bool = Query(False),
    db: Database = Depends(get_database),
):
    """Delete an account (soft-delete/disable by default)."""
    encryption, settings = _get_encryption_and_settings()
    manager = AccountManager(db, encryption, settings)

    try:
        # Check if exists
        account = manager.get_account(account_id)
        if not account:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f'Account with id {account_id} not found',
            )

        # Close active baskets before deletion if any
        if account.is_active:
            try:
                from execution.executor import SignalExecutor
                executor = SignalExecutor(db, manager, encryption, settings)
                executor.close_account_baskets(account_id, 'account_deleted')
            except Exception as e:
                logger.error('Failed to close baskets for account %s before delete: %s', account_id, e)

        success = manager.delete_account(account_id, hard=hard)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f'Account with id {account_id} not found',
            )

        msg = f'Account {account_id} permanently deleted' if hard else f'Account {account_id} deactivated'
        return SuccessResponse(message=msg)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to delete account: {e}',
        )


@router.post('/{account_id}/enable', response_model=AccountResponse)
async def enable_account(
    account_id: int,
    db: Database = Depends(get_database),
):
    """Enable / activate a trading account."""
    encryption, settings = _get_encryption_and_settings()
    manager = AccountManager(db, encryption, settings)

    account = manager.enable_account(account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Account with id {account_id} not found',
        )
    return _account_to_response(account, encryption)


@router.post('/{account_id}/disable', response_model=AccountResponse)
async def disable_account(
    account_id: int,
    db: Database = Depends(get_database),
):
    """Disable / deactivate a trading account and close any active baskets."""
    encryption, settings = _get_encryption_and_settings()
    manager = AccountManager(db, encryption, settings)

    # Disable first
    account = manager.disable_account(account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Account with id {account_id} not found',
        )

    # Close positions
    try:
        from execution.executor import SignalExecutor
        executor = SignalExecutor(db, manager, encryption, settings)
        executor.close_account_baskets(account_id, 'account_disabled')
    except Exception as e:
        logger.error('Failed to close baskets for deactivated account %s: %s', account_id, e)

    return _account_to_response(account, encryption)


@router.get('/{account_id}/balance')
async def get_account_balance(
    account_id: int,
    db: Database = Depends(get_database),
):
    """Sync and fetch live exchange balance for the account."""
    encryption, settings = _get_encryption_and_settings()
    manager = AccountManager(db, encryption, settings)

    try:
        balance = manager.get_account_balance(account_id)
        return balance
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f'Exchange communication failure: {e}',
        )


@router.get('/{account_id}/positions')
async def get_account_positions(
    account_id: int,
    db: Database = Depends(get_database),
):
    """Sync and fetch live exchange positions for the account."""
    encryption, settings = _get_encryption_and_settings()
    manager = AccountManager(db, encryption, settings)

    try:
        positions = manager.sync_account_positions(account_id)
        return positions
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f'Exchange communication failure: {e}',
        )
