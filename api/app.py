"""FastAPI application factory for the Zentry Market Analysis API.

    Next.js frontend  →  REST  →  THIS SERVICE
                                     ↓
                          Market Data Provider layer
                                     ↓
                          Analysis → Confluence → Signal Generator
                                     ↓
                                 JSON response

The service is read-only with respect to markets: it holds no exchange
credentials and exposes no order, balance, or position endpoint.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import analyze, health, markets
from api.routes.health import SERVICE_VERSION
from providers.base import ProviderError
from providers.registry import shutdown as shutdown_providers

logger = logging.getLogger(__name__)

API_PREFIX = '/api'


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format='%(asctime)s [%(name)s] %(levelname)-5s: %(message)s',
    )


def _allowed_origins() -> list[str]:
    """CORS origins from ALLOWED_ORIGINS (comma-separated).

    Defaults to local Next.js dev. Set it explicitly in production — the
    frontend is the only client that should reach this service directly.
    """
    raw = os.environ.get('ALLOWED_ORIGINS', 'http://localhost:3000')
    return [origin.strip() for origin in raw.split(',') if origin.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configuration at startup; release providers at shutdown."""
    from api.dependencies import get_settings

    settings = get_settings()          # raises on invalid config → fail fast
    _configure_logging(settings.log_level)
    logger.info(
        'Zentry Market Analysis API %s starting (trading disabled by design)',
        SERVICE_VERSION,
    )
    yield
    shutdown_providers()
    logger.info('Zentry Market Analysis API stopped')


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(
        title='Zentry Market Analysis API',
        version=SERVICE_VERSION,
        description=(
            'Analyses a market on request and returns one trading signal. '
            'This service never places, modifies, or closes an order.'
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=True,
        allow_methods=['GET', 'POST'],
        allow_headers=['*'],
    )

    @app.exception_handler(ProviderError)
    async def _provider_error_handler(_: Request, exc: ProviderError) -> JSONResponse:
        """Any provider failure that escaped a route becomes a clean 503."""
        logger.warning('Unhandled provider error: %s', exc)
        return JSONResponse(
            status_code=503,
            content={'error': 'market_data_unavailable', 'detail': str(exc)},
        )

    app.include_router(health.router)
    app.include_router(analyze.router, prefix=API_PREFIX)
    app.include_router(markets.router, prefix=API_PREFIX)

    return app


app = create_app()
