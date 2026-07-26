"""ZenGrid — Admin API Pydantic Schemas."""

from typing import Optional, Dict, Any, List
from datetime import datetime
from pydantic import BaseModel, Field


class AccountCreateRequest(BaseModel):
    """Request schema for creating a new trading account.

    Attributes:
        user_id: Owner user ID (defaults to admin user 1).
        label: Human-readable account label.
        api_key: Binance API key (will be encrypted at rest).
        api_secret: Binance API secret (will be encrypted at rest).
        is_active: Whether the account should be active immediately.
        use_testnet: Route orders to Binance testnet.
        risk_pct: Per-trade risk percentage of account balance.
        max_positions: Maximum simultaneous open positions.
        leverage_override: Fixed leverage (None = volatility-based).
        tp_settings: Custom take-profit configuration (JSONB).
        sl_settings: Custom stop-loss configuration (JSONB).
    """

    user_id: int = 1  # Default to admin user
    label: str = Field(..., min_length=1, max_length=100)
    api_key: str = Field(..., min_length=10)
    api_secret: str = Field(..., min_length=10)
    is_active: bool = True
    use_testnet: bool = False
    risk_pct: float = Field(default=0.02, ge=0.001, le=0.5)
    max_positions: int = Field(default=5, ge=1, le=50)
    leverage_override: Optional[int] = Field(default=None, ge=1, le=125)
    tp_settings: Optional[Dict[str, Any]] = None
    sl_settings: Optional[Dict[str, Any]] = None


class AccountUpdateRequest(BaseModel):
    """Request schema for partially updating a trading account.

    All fields are optional — only provided fields are updated.

    Attributes:
        label: Updated account label.
        api_key: New API key (re-encrypted at rest).
        api_secret: New API secret (re-encrypted at rest).
        is_active: Enable or disable the account.
        use_testnet: Switch testnet mode.
        risk_pct: Updated per-trade risk percentage.
        max_positions: Updated position limit.
        leverage_override: Updated fixed leverage.
        tp_settings: Updated take-profit config.
        sl_settings: Updated stop-loss config.
    """

    label: Optional[str] = Field(default=None, max_length=100)
    api_key: Optional[str] = Field(default=None, min_length=10)
    api_secret: Optional[str] = Field(default=None, min_length=10)
    is_active: Optional[bool] = None
    use_testnet: Optional[bool] = None
    risk_pct: Optional[float] = Field(default=None, ge=0.001, le=0.5)
    max_positions: Optional[int] = Field(default=None, ge=1, le=50)
    leverage_override: Optional[int] = Field(default=None, ge=1, le=125)
    tp_settings: Optional[Dict[str, Any]] = None
    sl_settings: Optional[Dict[str, Any]] = None


class AccountResponse(BaseModel):
    """Response schema for a trading account.

    API keys are never returned in plaintext — only a masked
    representation (``****last4``) is included.

    Attributes:
        id: Account primary key.
        user_id: Owner user ID.
        label: Human-readable account label.
        masked_api_key: Masked API key for display.
        is_active: Whether the account is currently active.
        use_testnet: Whether testnet mode is enabled.
        risk_pct: Per-trade risk percentage.
        max_positions: Maximum simultaneous positions.
        leverage_override: Fixed leverage override (None = dynamic).
        tp_settings: Take-profit configuration.
        sl_settings: Stop-loss configuration.
        cached_balance: Last synced USDT balance.
        last_sync_at: ISO timestamp of the last successful sync.
        created_at: ISO timestamp when the account was created.
        updated_at: ISO timestamp of the last account update.
    """

    id: int
    user_id: int
    label: str
    masked_api_key: str
    is_active: bool
    use_testnet: bool
    risk_pct: float
    max_positions: int
    leverage_override: Optional[int] = None
    tp_settings: Optional[Dict[str, Any]] = None
    sl_settings: Optional[Dict[str, Any]] = None
    cached_balance: float = 0.0
    last_sync_at: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = {'from_attributes': True}


class PositionResponse(BaseModel):
    """Response schema for an open or closed position.

    Attributes:
        id: Position primary key.
        account_id: Owning account ID.
        symbol: Trading pair (e.g. ``BTC/USDT:USDT``).
        side: Position direction (``long`` or ``short``).
        quantity: Position size in base currency.
        entry_price: Average entry price.
        unrealized_pnl: Current unrealised profit/loss.
        leverage: Leverage multiplier in effect.
        status: Position status (``open`` or ``closed``).
        opened_at: ISO timestamp when the position was opened.
    """

    id: int
    account_id: int
    symbol: str
    side: str
    quantity: float
    entry_price: float
    unrealized_pnl: float
    leverage: int
    status: str
    opened_at: str

    model_config = {'from_attributes': True}


class TradeResponse(BaseModel):
    """Response schema for a completed trade record.

    Attributes:
        id: Trade UUID.
        account_id: Owning account ID (may be None for legacy trades).
        basket_id: UUID of the basket this trade belongs to.
        symbol: Trading pair.
        side: Trade direction.
        entry_price: Weighted average entry price.
        exit_price: Exit fill price.
        quantity: Total quantity closed.
        margin: Total margin used across layers.
        leverage: Leverage multiplier.
        pnl: Realised profit/loss in USDT.
        fee: Total fees paid.
        layers_used: Number of recovery layers used.
        entry_time: Unix timestamp of the initial entry.
        exit_time: Unix timestamp of the exit.
        exit_reason: Why the trade was closed (TP, SL, etc.).
    """

    id: str
    account_id: Optional[int] = None
    basket_id: str
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    margin: float
    leverage: int
    pnl: float
    fee: float
    layers_used: int
    entry_time: float
    exit_time: float
    exit_reason: str

    model_config = {'from_attributes': True}


class TradeSummary(BaseModel):
    """Aggregated trade performance summary.

    Attributes:
        total_trades: Total number of closed trades.
        total_pnl: Sum of all realised PnL.
        total_fees: Sum of all fees paid.
        winning_trades: Number of trades with positive PnL.
        losing_trades: Number of trades with negative PnL.
        win_rate: Winning trades / total trades (0.0–1.0).
        avg_pnl: Average PnL per trade.
    """

    total_trades: int
    total_pnl: float
    total_fees: float
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_pnl: float


class ErrorResponse(BaseModel):
    """Standard error response body.

    Attributes:
        detail: Human-readable error description.
    """

    detail: str


class SuccessResponse(BaseModel):
    """Standard success response body.

    Attributes:
        message: Human-readable success description.
    """

    message: str
