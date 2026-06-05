"""ZenGrid — Admin API Dependencies.

FastAPI dependency injection for database sessions, authentication,
and service instances.
"""

import os
from typing import Generator

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from core.database import Database

# Singleton instances (initialized by app startup)
_database: Database | None = None
_api_key: str = ''

api_key_header = APIKeyHeader(name='X-API-Key', auto_error=False)


def init_dependencies(db: Database, admin_api_key: str) -> None:
    """Initialize dependency singletons. Called once at app startup."""
    global _database, _api_key
    _database = db
    _api_key = admin_api_key


def get_database() -> Database:
    """Get the Database instance."""
    if _database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Database not initialized',
        )
    return _database


def get_db_session(db: Database = Depends(get_database)) -> Generator[Session, None, None]:
    """Provide a transactional database session."""
    session = db.SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def verify_api_key(
    api_key: str | None = Security(api_key_header),
) -> str:
    """Verify the X-API-Key header."""
    if not _api_key:
        # No API key configured — allow all (dev mode)
        return 'dev'
    if not api_key or api_key != _api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Invalid or missing API key',
        )
    return api_key
