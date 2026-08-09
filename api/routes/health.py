"""Liveness and capability endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from analysis.cache import CANDLE_CACHE
from api.schemas import HealthResponse
from providers.registry import describe_providers

router = APIRouter(tags=['system'])

SERVICE_NAME = 'zentry-analysis-api'
SERVICE_VERSION = '1.1.0'


@router.get('/health', response_model=HealthResponse, summary='Service health')
def health() -> HealthResponse:
    """Report liveness without touching the network or the database.

    `trading_enabled` is hardcoded false and is asserted by the test suite: this
    service has no order path and must never gain one.
    """
    return HealthResponse(
        status='ok',
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        trading_enabled=False,
        providers_registered=[p['provider'] for p in describe_providers()],
        cache=CANDLE_CACHE.stats.as_dict(),
    )
