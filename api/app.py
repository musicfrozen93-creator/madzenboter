"""FastAPI application factory for the Zentry Market Analysis API.

    Browser  →  Next.js (the PUBLIC API boundary: authenticates the user,
                checks subscription, entitlement and rate limit)
                     ↓  server-to-server, carrying ANALYSIS_API_KEY
             THIS SERVICE (internal analysis engine)
                     ↓
             Market Data Provider layer
                     ↓
             Analysis → Confluence → Signal Generator
                     ↓
                 JSON response

THIS SERVICE IS AN INTERNAL ENGINE. It knows nothing about users, sessions or
subscriptions — those live in Next.js, which is the only client that should
reach it. Because the website runs on Vercel (serverless, external to the VPS),
this service must nonetheless be published on the public internet, so every
analysis route requires a shared service credential. See api/security.py.

CORS IS NOT AUTHENTICATION. It is a browser-enforced policy and does nothing to
curl, Postman, requests, or any server-side client. The origin allowlist below
restricts browser JavaScript only; the credential check is what actually keeps
anonymous callers out.

The service is read-only with respect to markets: it holds no exchange
credentials and exposes no order, balance, or position endpoint.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import analyze, health, markets
from api.security import API_KEY_ENV, is_configured, require_service_auth
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

    NOT a security control. CORS tells a BROWSER whether page JavaScript may
    read a cross-origin response; it is invisible to curl and to every
    server-side client, so it can neither grant nor deny access to this API.
    Authorization is `require_service_auth`, and nothing else.

    In the production topology no browser calls this service at all — Next.js
    does, server to server — so the allowlist exists only for local development
    and for anyone pointing a browser at /docs. A wildcard is refused because it
    would broadcast that this service expects anonymous browser traffic, which
    it does not.
    """
    raw = os.environ.get('ALLOWED_ORIGINS', 'http://localhost:3000')
    origins = [origin.strip() for origin in raw.split(',') if origin.strip()]
    if '*' in origins:
        logger.critical(
            "ALLOWED_ORIGINS contains '*'. Dropping it: this is a private "
            'engine service, not a public browser API.'
        )
        origins = [o for o in origins if o != '*']
    return origins


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
    # State the security posture at startup so a misconfigured deploy is
    # obvious in the first lines of the container log, not after an incident.
    if is_configured():
        logger.info(
            'Service authentication ENABLED — analysis routes require %s.',
            API_KEY_ENV,
        )
    else:
        logger.critical(
            'Service authentication is NOT configured (%s is unset or too '
            'short). Analysis routes will reject EVERY request with 503 until '
            'it is set. This is deliberate: serving anonymously would expose '
            'the engine, and every check the Next.js layer performs, to the '
            'public internet.',
            API_KEY_ENV,
        )
    yield
    shutdown_providers()
    logger.info('Zentry Market Analysis API stopped')


def _docs_enabled() -> bool:
    """Whether to serve the interactive API documentation.

    Off in production. /docs, /redoc and /openapi.json are unauthenticated by
    design — FastAPI mounts them outside the router dependencies — so in
    production they would hand any anonymous visitor a complete map of the
    routes, request schemas and field names behind the credential wall. That
    does not grant access, but there is no reason to publish the blueprint.

    Controlled by ENVIRONMENT / ENV / NODE_ENV: anything starting with "prod"
    disables them. Set ENABLE_DOCS=true to force them on (useful for a staging
    box), or ENABLE_DOCS=false to force them off anywhere.
    """
    override = (os.environ.get('ENABLE_DOCS') or '').strip().lower()
    if override in ('1', 'true', 'yes'):
        return True
    if override in ('0', 'false', 'no'):
        return False
    env = (
        os.environ.get('ENVIRONMENT')
        or os.environ.get('ENV')
        or os.environ.get('NODE_ENV')
        or 'development'
    ).strip().lower()
    return not env.startswith('prod')


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    docs = _docs_enabled()
    app = FastAPI(
        title='Zentry Market Analysis API',
        version=SERVICE_VERSION,
        description=(
            'Analyses a market on request and returns one trading signal. '
            'This service never places, modifies, or closes an order.'
        ),
        lifespan=lifespan,
        # None removes the route entirely — a 404, not an empty page.
        docs_url='/docs' if docs else None,
        redoc_url='/redoc' if docs else None,
        openapi_url='/openapi.json' if docs else None,
    )
    if not docs:
        logger.info('Interactive API docs disabled (production).')

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

    # /health stays open: the Docker HEALTHCHECK and any uptime monitor call it
    # unauthenticated, and it discloses nothing beyond liveness and version.
    app.include_router(health.router)

    # Everything that performs analysis or describes the engine's capabilities
    # requires the service credential.
    protected = [Depends(require_service_auth)]
    app.include_router(analyze.router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(markets.router, prefix=API_PREFIX, dependencies=protected)

    return app


app = create_app()
