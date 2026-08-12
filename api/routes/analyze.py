"""POST /api/analyze — the one endpoint the dashboard calls."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from analysis.modules import resolve_enabled_modules
from analysis.pipeline import SignalPipeline
from api.dependencies import get_pipeline, get_settings
from api.schemas import AnalyzeRequest, AnalyzeResponse, ErrorResponse
from api.serializers import to_analyze_response
from config.settings import Settings
from providers.base import (
    TRADEABLE_TIMEFRAMES,
    MarketDataUnavailableError,
    UnknownSymbolError,
    UnsupportedTimeframeError,
    normalize_symbol,
    normalize_timeframe,
)
from providers.registry import UnknownProviderError, get_provider

logger = logging.getLogger(__name__)

router = APIRouter(tags=['analysis'])


@router.post(
    '/analyze',
    response_model=AnalyzeResponse,
    summary='Analyse one market and return a single trading signal',
    responses={
        400: {'model': ErrorResponse, 'description': 'Bad symbol, timeframe, or market'},
        422: {'model': ErrorResponse, 'description': 'Not enough history to analyse'},
        503: {'model': ErrorResponse, 'description': 'Market data unavailable'},
    },
)
def analyze(
    payload: AnalyzeRequest,
    pipeline: SignalPipeline = Depends(get_pipeline),
    settings: Settings = Depends(get_settings),
) -> AnalyzeResponse:
    """Run the full pipeline for one symbol on one timeframe.

    The timeframe in the request is the one the user trades. Higher timeframes
    are analysed internally for trend and structure confirmation and are never
    returned — the response describes the selected timeframe only.

    A `WAIT` result is a successful response, not an error: it means no
    high-quality setup exists right now. Errors are reserved for genuine
    failures — an unknown symbol, an unsupported timeframe, or a venue outage.
    """
    # Validate at the edge so a malformed symbol never reaches a venue.
    try:
        symbol = normalize_symbol(payload.symbol)
        timeframe = normalize_timeframe(payload.timeframe)
    except (UnknownSymbolError, UnsupportedTimeframeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    # Product restriction: only the four tradeable entry timeframes may be
    # requested. Higher timeframes remain available to the engine internally.
    if timeframe not in TRADEABLE_TIMEFRAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f'Timeframe {timeframe!r} is not selectable. '
                f'Choose one of: {", ".join(TRADEABLE_TIMEFRAMES)}.'
            ),
        )

    try:
        provider = get_provider(settings, payload.market, payload.provider)
    except UnknownProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.exception('Provider init failed for market=%s', payload.market)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f'Market data provider unavailable: {exc}',
        ) from exc

    # Resolve the user's optional indicator selection against the module
    # allowlist. Unknown names are ignored and required modules are always kept,
    # so a client can never name an arbitrary internal function.
    enabled_modules = resolve_enabled_modules(payload.enabled_indicators)

    try:
        result = pipeline.run(provider, symbol, timeframe, enabled_modules=enabled_modules)
    except (UnknownSymbolError, UnsupportedTimeframeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except ValueError as exc:
        # Not enough candle history for the indicator set. Uses the literal 422
        # because Starlette renamed the constant across versions.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MarketDataUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return to_analyze_response(result)
