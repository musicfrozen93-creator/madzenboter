"""
ZenGrid — SQLAlchemy ORM Models.

Defines all database tables for the multi-account trading platform.
Uses PostgreSQL-specific types (JSONB) where appropriate.

Tables:
    users, accounts, positions, trades, signals, risk_metrics,
    subscriptions, execution_logs, baskets, recovery_layers,
    watchlist, bot_state, daily_stats
"""

import time
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""
    pass


# ─────────────────────────────────────────────
# User & Subscription
# ─────────────────────────────────────────────

class UserModel(Base):
    """Platform user — can own multiple trading accounts."""
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    accounts = relationship('AccountModel', back_populates='user', cascade='all, delete-orphan')
    subscriptions = relationship('SubscriptionModel', back_populates='user', cascade='all, delete-orphan')

    def __repr__(self) -> str:
        return f'<User id={self.id} username={self.username!r}>'


class SubscriptionModel(Base):
    """Subscription plan tracking for a user."""
    __tablename__ = 'subscriptions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    plan = Column(String(50), nullable=False, default='free')  # 'free', 'basic', 'pro', 'enterprise'
    status = Column(String(20), nullable=False, default='active')  # 'active', 'paused', 'cancelled', 'expired'
    max_accounts = Column(Integer, nullable=False, default=1)
    started_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    user = relationship('UserModel', back_populates='subscriptions')

    def __repr__(self) -> str:
        return f'<Subscription id={self.id} plan={self.plan!r} status={self.status!r}>'


# ─────────────────────────────────────────────
# Account
# ─────────────────────────────────────────────

class AccountModel(Base):
    """A trading account with encrypted Binance API credentials.

    Each account has its own risk settings, balance tracking, and
    independent position management. API keys are Fernet-encrypted.
    """
    __tablename__ = 'accounts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    label = Column(String(100), nullable=False)
    encrypted_api_key = Column(Text, nullable=False)
    encrypted_api_secret = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    use_testnet = Column(Boolean, default=False, nullable=False)

    # Per-account risk settings (overrides global)
    risk_pct = Column(Float, default=0.02, nullable=False)  # 2% default risk per trade
    max_positions = Column(Integer, default=5, nullable=False)
    leverage_override = Column(Integer, nullable=True)  # None = use global volatility-based

    # Per-account TP/SL settings (JSONB for flexibility)
    tp_settings = Column(JSONB, nullable=True)  # e.g. {'basket_tp_roi': {'low': 0.08, ...}}
    sl_settings = Column(JSONB, nullable=True)  # e.g. {'basket_sl_pct': 0.20, ...}

    # Cached balance (updated by sync service)
    cached_balance = Column(Float, default=0.0, nullable=False)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    user = relationship('UserModel', back_populates='accounts')
    positions = relationship('PositionModel', back_populates='account', cascade='all, delete-orphan')
    trades = relationship('TradeModel', back_populates='account', cascade='all, delete-orphan')
    baskets = relationship('BasketModel', back_populates='account', cascade='all, delete-orphan')
    risk_metrics = relationship('RiskMetricModel', back_populates='account', cascade='all, delete-orphan')
    execution_logs = relationship('ExecutionLogModel', back_populates='account', cascade='all, delete-orphan')
    daily_stats = relationship('DailyStatModel', back_populates='account', cascade='all, delete-orphan')

    def __repr__(self) -> str:
        return f'<Account id={self.id} label={self.label!r} active={self.is_active}>'


# ─────────────────────────────────────────────
# Position
# ─────────────────────────────────────────────

class PositionModel(Base):
    """Tracked open position for an account (synced from exchange)."""
    __tablename__ = 'positions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # 'long' or 'short'
    quantity = Column(Float, nullable=False, default=0.0)
    entry_price = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    leverage = Column(Integer, nullable=False, default=1)
    liquidation_price = Column(Float, nullable=True)
    status = Column(String(20), nullable=False, default='open', index=True)  # 'open', 'closed'
    opened_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    closed_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    account = relationship('AccountModel', back_populates='positions')

    __table_args__ = (
        Index('idx_positions_account_status', 'account_id', 'status'),
    )

    def __repr__(self) -> str:
        return f'<Position id={self.id} {self.symbol} {self.side} qty={self.quantity}>'


# ─────────────────────────────────────────────
# Signal
# ─────────────────────────────────────────────

class SignalModel(Base):
    """Persisted signal record for audit trail."""
    __tablename__ = 'signals'

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    strength = Column(Float, nullable=False)
    atr = Column(Float, nullable=False)
    market_regime = Column(String(20), nullable=False)
    volatility = Column(String(20), nullable=False)
    current_price = Column(Float, nullable=False)
    ema200 = Column(Float, nullable=False)
    rsi = Column(Float, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Relationships
    execution_logs = relationship('ExecutionLogModel', back_populates='signal')

    def __repr__(self) -> str:
        return f'<Signal id={self.id} {self.side} {self.symbol} str={self.strength:.2f}>'


# ─────────────────────────────────────────────
# Trade
# ─────────────────────────────────────────────

class TradeModel(Base):
    """Immutable record of a completed trade (basket close), per-account."""
    __tablename__ = 'trades'

    id = Column(String(36), primary_key=True)  # UUID string
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=True, index=True)
    basket_id = Column(String(36), nullable=False, index=True)
    signal_id = Column(Integer, ForeignKey('signals.id', ondelete='SET NULL'), nullable=True)
    symbol = Column(String(50), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    margin = Column(Float, nullable=False)
    leverage = Column(Integer, nullable=False)
    pnl = Column(Float, nullable=False)
    fee = Column(Float, nullable=False)
    layers_used = Column(Integer, nullable=False)
    entry_time = Column(Float, nullable=False)
    exit_time = Column(Float, nullable=False, index=True)
    exit_reason = Column(String(30), nullable=False)

    # Relationships
    account = relationship('AccountModel', back_populates='trades')

    def __repr__(self) -> str:
        return f'<Trade id={self.id[:8]} {self.symbol} pnl={self.pnl:.4f}>'


# ─────────────────────────────────────────────
# Risk Metrics
# ─────────────────────────────────────────────

class RiskMetricModel(Base):
    """Per-account risk metric snapshot, updated periodically."""
    __tablename__ = 'risk_metrics'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    daily_loss = Column(Float, nullable=False, default=0.0)
    max_drawdown = Column(Float, nullable=False, default=0.0)
    current_exposure = Column(Float, nullable=False, default=0.0)
    high_water_mark = Column(Float, nullable=False, default=0.0)
    open_positions_count = Column(Integer, nullable=False, default=0)
    daily_start_balance = Column(Float, nullable=False, default=0.0)
    current_balance = Column(Float, nullable=False, default=0.0)
    snapshot_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Relationships
    account = relationship('AccountModel', back_populates='risk_metrics')

    def __repr__(self) -> str:
        return f'<RiskMetric account={self.account_id} exposure={self.current_exposure:.2f}>'


# ─────────────────────────────────────────────
# Execution Log
# ─────────────────────────────────────────────

class ExecutionLogModel(Base):
    """Audit trail for every trade execution attempt per account."""
    __tablename__ = 'execution_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    signal_id = Column(Integer, ForeignKey('signals.id', ondelete='SET NULL'), nullable=True, index=True)
    action = Column(String(30), nullable=False)  # 'open', 'close', 'sync'
    symbol = Column(String(50), nullable=False)
    side = Column(String(10), nullable=True)
    quantity = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    status = Column(String(20), nullable=False, default='pending')  # 'pending', 'success', 'failed', 'skipped'
    error_message = Column(Text, nullable=True)
    executed_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Relationships
    account = relationship('AccountModel', back_populates='execution_logs')
    signal = relationship('SignalModel', back_populates='execution_logs')

    def __repr__(self) -> str:
        return f'<ExecLog id={self.id} account={self.account_id} {self.action} {self.status}>'


# ─────────────────────────────────────────────
# Basket (per-account, migrated from SQLite)
# ─────────────────────────────────────────────

class BasketModel(Base):
    """Position record — holds the single entry layer for a symbol/direction, per-account."""
    __tablename__ = 'baskets'

    id = Column(String(36), primary_key=True)  # UUID string
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=True, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    atr_at_entry = Column(Float, nullable=False)
    volatility = Column(String(20), nullable=False)
    leverage = Column(Integer, nullable=False, default=10)
    status = Column(String(20), nullable=False, default='active', index=True)
    created_at = Column(Float, nullable=False)  # Unix timestamp (matches existing schema)

    # Relationships
    account = relationship('AccountModel', back_populates='baskets')
    layers = relationship('RecoveryLayerModel', back_populates='basket', cascade='all, delete-orphan')

    __table_args__ = (
        Index('idx_baskets_account_status', 'account_id', 'status'),
    )

    def __repr__(self) -> str:
        return f'<Basket id={self.id[:8]} {self.symbol} {self.side} {self.status}>'


class RecoveryLayerModel(Base):
    """The single entry layer of a position. Table name retained for schema compatibility."""
    __tablename__ = 'recovery_layers'

    id = Column(Integer, primary_key=True, autoincrement=True)
    basket_id = Column(String(36), ForeignKey('baskets.id', ondelete='CASCADE'), nullable=False, index=True)
    layer_number = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    margin = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    side = Column(String(10), nullable=False)
    timestamp = Column(Float, nullable=False)  # Unix timestamp (matches existing schema)
    status = Column(String(20), nullable=False, default='active')

    # Relationships
    basket = relationship('BasketModel', back_populates='layers')

    def __repr__(self) -> str:
        return f'<Layer L{self.layer_number} basket={self.basket_id[:8]} {self.status}>'


# ─────────────────────────────────────────────
# Watchlist (global, not per-account)
# ─────────────────────────────────────────────

class WatchlistModel(Base):
    """Scanned coin with composite quality score — shared across all accounts."""
    __tablename__ = 'watchlist'

    symbol = Column(String(50), primary_key=True)
    volume_24h = Column(Float, nullable=True)
    atr = Column(Float, nullable=True)
    atr_score = Column(Float, nullable=True)
    volume_score = Column(Float, nullable=True)
    spread_score = Column(Float, nullable=True)
    funding_rate = Column(Float, nullable=True)
    funding_score = Column(Float, nullable=True)
    composite_score = Column(Float, nullable=True)
    updated_at = Column(Float, nullable=True)  # Unix timestamp

    def __repr__(self) -> str:
        return f'<Watchlist {self.symbol} score={self.composite_score}>'


# ─────────────────────────────────────────────
# Bot State (global KV store)
# ─────────────────────────────────────────────

class BotStateModel(Base):
    """Key-value store for global bot state (emergency shutdown, HWM, etc.)."""
    __tablename__ = 'bot_state'

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(Float, nullable=True)  # Unix timestamp

    def __repr__(self) -> str:
        return f'<BotState {self.key}={self.value!r}>'


# ─────────────────────────────────────────────
# Daily Stats (per-account)
# ─────────────────────────────────────────────

class DailyStatModel(Base):
    """Daily performance statistics, now per-account."""
    __tablename__ = 'daily_stats'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=True, index=True)
    date = Column(String(10), nullable=False)  # 'YYYY-MM-DD'
    starting_balance = Column(Float, nullable=True, default=0)
    ending_balance = Column(Float, nullable=True, default=0)
    realized_pnl = Column(Float, nullable=True, default=0)
    total_trades = Column(Integer, nullable=True, default=0)
    winning_trades = Column(Integer, nullable=True, default=0)
    losing_trades = Column(Integer, nullable=True, default=0)
    max_drawdown = Column(Float, nullable=True, default=0)
    created_at = Column(Float, nullable=True)  # Unix timestamp

    # Relationships
    account = relationship('AccountModel', back_populates='daily_stats')

    __table_args__ = (
        UniqueConstraint('account_id', 'date', name='uq_daily_stats_account_date'),
    )

    def __repr__(self) -> str:
        return f'<DailyStat account={self.account_id} date={self.date} pnl={self.realized_pnl}>'
