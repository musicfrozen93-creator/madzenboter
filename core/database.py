"""
Zentry — PostgreSQL Database Repository (SQLAlchemy).

Persistence for the AI Trading Signal platform: schema bootstrap plus the
signals audit trail. The website (zentry-web) shares this database and reads
users/subscriptions directly.

Everything that persisted an automatically opened position — baskets, recovery
layers, trades, watchlist scores, per-account risk metrics, execution logs,
daily stats, connected exchange accounts, and the bot_state key/value store —
was removed in the Phase 0 conversion. The tables are left untouched in existing
databases; see the Phase 0 report for the obsolete-table list.
"""

import logging
import os
from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.models import Base, SignalModel

logger = logging.getLogger(__name__)


class Database:
    """PostgreSQL repository for the signal platform's persistence needs."""

    def __init__(self, db_url: Optional[str] = None) -> None:
        """Initialise database connection.

        Args:
            db_url: PostgreSQL connection URL. Falls back to DATABASE_URL
                    env var, then to a local default.
        """
        self.db_url = (
            db_url
            or os.environ.get('DATABASE_URL')
            or 'postgresql://zengrid:zengrid@localhost:5432/zengrid'
        )

        self.engine = create_engine(
            self.db_url,
            pool_size=20,
            max_overflow=30,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=False,
        )
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    # ───────────────────────────────────────────
    # Session Management
    # ───────────────────────────────────────────

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """Provide a transactional session scope.

        Yields:
            SQLAlchemy Session that auto-commits on success, rolls back on error.
        """
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_session(self) -> Session:
        """Get a new session (caller is responsible for commit/close).

        Returns:
            New SQLAlchemy Session.
        """
        return self.SessionLocal()

    # ───────────────────────────────────────────
    # Schema Initialisation
    # ───────────────────────────────────────────

    def initialize(self) -> None:
        """Create all tables if they do not exist."""
        Base.metadata.create_all(bind=self.engine)
        logger.info('Database initialised at %s', self.db_url.split('@')[-1])

    # ───────────────────────────────────────────
    # Signal Operations
    # ───────────────────────────────────────────

    def save_signal(self, signal) -> int:
        """Persist an analysis result for the audit trail.

        Args:
            signal: Signal DTO from the analysis layer.

        Returns:
            The auto-generated signal ID.
        """
        with self.session() as session:
            sig = SignalModel(
                symbol=signal.symbol,
                side=signal.side,
                strength=signal.strength,
                atr=signal.atr,
                market_regime=signal.market_regime,
                volatility=signal.volatility,
                current_price=signal.current_price,
                ema200=signal.ema200,
                rsi=signal.rsi,
            )
            session.add(sig)
            session.flush()
            return sig.id

    # ───────────────────────────────────────────
    # Lifecycle
    # ───────────────────────────────────────────

    def close(self) -> None:
        """Dispose of the connection pool."""
        self.engine.dispose()
        logger.info('Database connection closed')
