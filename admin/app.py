"""ZenGrid — Admin REST API Application.

Provides REST endpoints for managing accounts, viewing positions and trades.
Authenticated via X-API-Key header.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from admin.dependencies import init_dependencies
from admin.routes import accounts, positions, trades
from core.database import Database

logger = logging.getLogger(__name__)


def create_app(database: Database, admin_api_key: str = '') -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        database: Initialized Database instance.
        admin_api_key: API key for authentication.

    Returns:
        Configured FastAPI app.
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_dependencies(database, admin_api_key)
        logger.info('Admin API started')
        yield
        logger.info('Admin API shutting down')

    app = FastAPI(
        title='ZenGrid Admin API',
        description='Multi-account trading platform administration',
        version='1.0.0',
        lifespan=lifespan,
    )

    app.include_router(accounts.router, prefix='/accounts', tags=['Accounts'])
    app.include_router(positions.router, prefix='/positions', tags=['Positions'])
    app.include_router(trades.router, prefix='/trades', tags=['Trades'])

    @app.get('/health')
    async def health_check():
        return {'status': 'ok'}

    return app
