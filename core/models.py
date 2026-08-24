"""
Zentry — SQLAlchemy ORM Models.

Defines the database tables the AI Trading Signal platform owns:

    users, subscriptions, signals

The website (zentry-web) shares this database and adds its own web-only tables
via lib/db/migration_shared.sql; `users` and `subscriptions` are created here and
extended there.

The auto-trading tables (accounts, positions, trades, risk_metrics,
execution_logs, baskets, recovery_layers, watchlist, bot_state, daily_stats)
were dropped from this model set in the Phase 0 conversion because nothing
opens, tracks, or closes a position any more. The tables themselves are left
untouched in existing databases — see the Phase 0 report for the obsolete-table
list before deciding to drop them.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""
    pass


# ─────────────────────────────────────────────
# User & Subscription
# ─────────────────────────────────────────────

class UserModel(Base):
    """Platform user."""
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
# Signal
# ─────────────────────────────────────────────

class SignalModel(Base):
    """Persisted record of an analysis result, for the audit trail."""
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

    def __repr__(self) -> str:
        return f'<Signal id={self.id} {self.side} {self.symbol} str={self.strength:.2f}>'


# ─────────────────────────────────────────────
# Signal Outcome Tracking (Phase 1 F7)
# ─────────────────────────────────────────────

class SignalOutcomeModel(Base):
    """Tracks the lifecycle of every actionable signal the engine emits."""
    __tablename__ = 'signal_outcomes'

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    market = Column(String(20), nullable=False)
    provider = Column(String(30), nullable=False)
    direction = Column(String(4), nullable=False)
    entry = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    tp1 = Column(Float, nullable=False)
    tp2 = Column(Float, nullable=False)
    tp3 = Column(Float, nullable=False)
    risk_reward = Column(Float, nullable=False)
    quality = Column(Integer, nullable=False)
    confidence = Column(Integer, nullable=False)
    snapshot = Column(Text, nullable=False)
    status = Column(
        String(20), nullable=False, default='OPEN', index=True,
    )
    opened_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    closed_at = Column(DateTime(timezone=True), nullable=True)
    close_price = Column(Float, nullable=True)
    pnl_r = Column(Float, nullable=True)

    def __repr__(self) -> str:
        return f'<SignalOutcome id={self.id} {self.direction} {self.symbol} status={self.status}>'
