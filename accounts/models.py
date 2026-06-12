"""ZenGrid — Account Pydantic Schemas & Model Re-exports."""

from typing import Optional, Dict, Any
from pydantic import BaseModel, Field

# Re-export ORM models for convenience
from core.models import AccountModel, UserModel, SubscriptionModel


class AccountCreate(BaseModel):
    """Schema for creating a new trading account."""
    user_id: int
    label: str = Field(..., min_length=1, max_length=100)
    api_key: str = Field(..., min_length=10)
    api_secret: str = Field(..., min_length=10)
    is_active: bool = True
    use_testnet: bool = False
    risk_pct: float = Field(default=0.02, ge=0.001, le=0.5)
    max_positions: int = Field(default=8, ge=1, le=50)
    leverage_override: Optional[int] = Field(default=None, ge=1, le=125)
    tp_settings: Optional[Dict[str, Any]] = None
    sl_settings: Optional[Dict[str, Any]] = None


class AccountUpdate(BaseModel):
    """Schema for updating an existing account."""
    label: Optional[str] = Field(default=None, min_length=1, max_length=100)
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
    """Schema for account API responses (keys masked)."""
    id: int
    user_id: int
    label: str
    masked_api_key: str
    is_active: bool
    use_testnet: bool
    risk_pct: float
    max_positions: int
    leverage_override: Optional[int]
    tp_settings: Optional[Dict[str, Any]]
    sl_settings: Optional[Dict[str, Any]]
    cached_balance: float
    last_sync_at: Optional[str]
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True
