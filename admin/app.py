"""ZenGrid — Admin REST API Application.

Provides REST endpoints for managing accounts, viewing positions, trades,
and bot control operations. Authenticated via X-API-Key header.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from admin.dependencies import init_dependencies
from admin.routes import accounts, positions, trades
from admin.routes import bot_control as bot_control_routes
from control.bot_control import BotControl
from core.database import Database

logger = logging.getLogger(__name__)

# Resolve the dashboard HTML path relative to this file so it works
# regardless of the working directory the server is launched from.
_DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), 'static', 'dashboard.html')


def create_app(
    database: Database,
    admin_api_key: str = '',
    bot_control: Optional[BotControl] = None,
    signal_executor=None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        database: Initialized Database instance.
        admin_api_key: API key for authentication.
        bot_control: BotControl singleton for runtime control.
        signal_executor: SignalExecutor for force-close / order-cancel operations.

    Returns:
        Configured FastAPI app.
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_dependencies(database, admin_api_key, bot_control, signal_executor)
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
    app.include_router(bot_control_routes.router, prefix='/api/admin', tags=['Bot Control'])

    @app.get('/health')
    async def health_check():
        return {'status': 'ok'}

    @app.get('/admin/dashboard', response_class=HTMLResponse, include_in_schema=False)
    async def admin_dashboard():
        """Serve the admin control panel HTML dashboard."""
        try:
            with open(_DASHBOARD_PATH, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            return HTMLResponse(
                content='<h1>Dashboard not found</h1>'
                        '<p>admin/static/dashboard.html is missing.</p>',
                status_code=404,
            )

    return app
