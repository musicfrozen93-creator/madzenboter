"""Discovery endpoints — what this service can analyse.

The frontend uses these to populate the market / pair / timeframe selectors, so
the dashboard never hardcodes a symbol list.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.dependencies import get_settings
from api.schemas import ErrorResponse, MarketsResponse, ProviderModel, SymbolsResponse
from config.settings import Settings
from providers.registry import (
    UnknownProviderError,
    available_markets,
    describe_providers,
    get_provider,
)

router = APIRouter(tags=['discovery'])


@router.get(
    '/markets',
    response_model=MarketsResponse,
    summary='List every registered market and provider',
)
def list_markets(settings: Settings = Depends(get_settings)) -> MarketsResponse:
    """Registered markets and the providers serving each.

    Does not touch the network: capability metadata only.
    """
    return MarketsResponse(
        markets=available_markets(),
        providers=[ProviderModel(**p) for p in describe_providers()],
    )


@router.get(
    '/markets/{market}/symbols',
    response_model=SymbolsResponse,
    summary='List every symbol a market offers',
    responses={
        400: {'model': ErrorResponse, 'description': 'Unknown market or provider'},
        503: {'model': ErrorResponse, 'description': 'Market data unavailable'},
    },
)
def list_symbols(
    market: str,
    provider: str | None = Query(default=None, description='Optional provider override'),
    settings: Settings = Depends(get_settings),
) -> SymbolsResponse:
    """Every symbol and timeframe available for a market."""
    try:
        instance = get_provider(settings, market, provider)
    except UnknownProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f'Market data provider unavailable: {exc}',
        ) from exc

    symbols = instance.list_symbols()
    return SymbolsResponse(
        market=instance.market,
        provider=instance.name,
        timeframes=list(instance.supported_timeframes()),
        symbols=symbols,
        count=len(symbols),
    )
