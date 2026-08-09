"""Shared FastAPI dependencies: settings, the pipeline, and provider resolution.

Settings and the pipeline are process-wide singletons — both are stateless and
expensive enough to build that per-request construction would be wasteful.
"""

from __future__ import annotations

import functools
import logging
import os

from fastapi import Depends, HTTPException, status

from analysis.pipeline import SignalPipeline
from config.settings import Settings
from providers.base import MarketDataProvider
from providers.registry import UnknownProviderError, get_provider

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.environ.get('ZENTRY_CONFIG', 'config/config.json')


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache application settings.

    Raises:
        RuntimeError: If the config file is missing or fails validation, so the
            service fails fast at startup rather than per request.
    """
    settings = Settings.load(DEFAULT_CONFIG_PATH)
    issues = settings.validate()
    if issues:
        raise RuntimeError(
            'Invalid configuration: ' + '; '.join(issues)
        )
    return settings


@functools.lru_cache(maxsize=1)
def get_pipeline() -> SignalPipeline:
    """Build and cache the signal pipeline."""
    return SignalPipeline(get_settings())


def resolve_provider(
    market: str,
    provider: str | None = None,
    settings: Settings = Depends(get_settings),
) -> MarketDataProvider:
    """Resolve (market, provider) to an initialized provider, or 400/503.

    Raises:
        HTTPException: 400 when the market/provider is unknown, 503 when the
            venue could not be reached during initialization.
    """
    try:
        return get_provider(settings, market, provider)
    except UnknownProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.exception('Provider initialization failed for market=%s', market)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f'Market data provider unavailable: {exc}',
        ) from exc
