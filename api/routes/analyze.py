"""POST /api/analyze — the one endpoint the dashboard calls."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from analysis.modules import (
    UnknownPresetError,
    preset_modules,
    resolve_enabled_modules,
)
from analysis.strategies import (
    CUSTOM_STRATEGY_ID,
    DEFAULT_STRATEGY_ID,
    STRATEGIES,
    get_strategy,
    strategy_modules,
)
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

    # Resolve the requested configuration, in strict priority order:
    #
    #   1. a known strategy_id      → that strategy, from the registry
    #   2. an unknown strategy_id   → 400 (never substituted — that fails open)
    #   3. enabled_indicators       → the caller's explicit module list (custom)
    #   4. a legacy preset id       → that preset's modules
    #   5. nothing at all           → the production default, ICT + MSNR
    #
    # Unknown module names are ignored and required core modules are always
    # kept, so a client can never name an arbitrary internal function.
    #
    # Case 5 is what an older client sends — the Telegram bot and any external
    # consumer written before strategies existed. It used to mean "every
    # module", but that set matches no registered strategy, so under the
    # production-safety policy it would resolve to CUSTOM and never trade.
    # An unconfigured caller gets the production strategy instead, which is
    # what they were effectively asking for: a tradeable signal from the
    # methodology this product actually stands behind.
    #
    # Note this is the default for an UNCONFIGURED request only. A caller who
    # sends an explicit module list still gets exactly that list, and it is
    # still CUSTOM — omission is the only thing that changes.
    strategy = get_strategy(payload.strategy_id)
    requested_named_strategy = bool(
        payload.strategy_id
        and str(payload.strategy_id).strip().upper() != CUSTOM_STRATEGY_ID
    )
    if strategy is not None:
        # A NAMED strategy is resolved from the registry and wins over any
        # module list sent with it: the client names a strategy, the backend
        # decides what that means.
        enabled_modules = strategy.modules
    elif requested_named_strategy:
        # An id we do not recognise is REJECTED, never substituted. Falling back
        # to the default here would fail OPEN: a request for MOMENTUM_V2 — a
        # typo, a renamed id, a stale client — would silently return a tradeable
        # production signal for a strategy the product considers non-tradeable.
        # A name the registry does not define is a client error.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f'Unknown strategy_id {payload.strategy_id!r}. '
                f'Choose one of: {", ".join(sorted(STRATEGIES))}, '
                f'or {CUSTOM_STRATEGY_ID} with enabled_indicators.'
            ),
        )
    elif payload.enabled_indicators is not None:
        enabled_modules = resolve_enabled_modules(payload.enabled_indicators)
    elif payload.preset:
        # Same rule for the legacy field: an unrecognised preset is an error,
        # not a licence to run the production strategy.
        try:
            enabled_modules = preset_modules(payload.preset)
        except UnknownPresetError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
            ) from exc
    else:
        enabled_modules = strategy_modules(DEFAULT_STRATEGY_ID)

    try:
        result = pipeline.run(
            provider, symbol, timeframe,
            enabled_modules=enabled_modules,
            # What the caller SAID they were running. Consulted only to make
            # the verdict more restrictive: an explicit CUSTOM stays CUSTOM
            # even if its modules happen to equal a registered strategy's.
            declared_strategy_id=payload.strategy_id,
        )
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
