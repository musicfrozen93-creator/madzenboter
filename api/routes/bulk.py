"""POST /api/analyze/bulk — the same analysis, many symbols, one request.

This route is a MULTI-SYMBOL EXECUTION LAYER, not a second signal engine. It
validates a batch, resolves one provider and one strategy, and hands the work to
``analysis.bulk.run_bulk``, which runs the production single-symbol path per
symbol. The full single-analysis payload comes back embedded, untouched: this
module never reads, re-derives or rewrites a signal, a score, or a level.

WHY THE VALIDATION IS STRICTER HERE THAN ON /api/analyze
    One analysis is one venue conversation the user asked for and is watching.
    A batch multiplies everything — venue load, wall time, and the blast radius
    of a misconfigured client — so the batch route gives up the conveniences the
    single route offers:

      * NO DEFAULT STRATEGY. The single route treats an unconfigured request as
        "give me the production default", because an older client that predates
        strategies is still a human asking for one signal. A batch caller is a
        program; a program that forgot to name a strategy has a bug, and running
        fifty analyses under a guess is the wrong way to find out.
      * NO MODULE LIST. Bulk names a strategy and the backend decides what it
        contains. There is no path by which a batch caller assembles its own
        module set.
      * PRODUCTION ONLY. An experimental or custom configuration is forced to
        WAIT by the decision layer anyway, so a fifty-symbol scan of one would
        burn the venue budget to produce fifty non-answers.
      * A HARD SYMBOL CEILING, independent of whatever the caller in front of
        this service believes its own limit to be.

    Ownership, subscription and per-user rate limiting are NOT checked here and
    never will be: this service knows nothing about users. The Next.js layer is
    the public API boundary and owns all of that.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from analysis.bulk import (
    BULK_BLOCKED_MARKETS,
    MAX_BULK_SYMBOLS_PER_REQUEST,
    STATUS_COMPLETED,
    run_bulk,
)
from analysis.pipeline import SignalPipeline
from analysis.strategies import (
    CUSTOM_STRATEGY_ID,
    STRATEGIES,
    StrategyStatus,
    get_strategy,
)
from api.dependencies import get_pipeline, get_settings
from api.schemas import (
    BulkAnalyzeRequest,
    BulkAnalyzeResponse,
    BulkResultModel,
    ErrorResponse,
)
from api.serializers import to_analyze_response
from config.settings import Settings
from providers.base import (
    TRADEABLE_TIMEFRAMES,
    UnknownSymbolError,
    UnsupportedTimeframeError,
    normalize_symbol,
    normalize_timeframe,
)
from providers.registry import UnknownProviderError, get_provider

logger = logging.getLogger(__name__)

router = APIRouter(tags=['analysis'])


def _production_strategy_ids() -> List[str]:
    """The strategy ids bulk analysis will accept, read from the registry.

    Derived rather than hardcoded so that promoting a strategy to production is
    a one-line change in ``analysis/strategies.py`` and nothing here moves.

    Returns:
        Sorted production strategy ids.
    """
    return sorted(
        s.strategy_id for s in STRATEGIES.values()
        if s.status == StrategyStatus.PRODUCTION
    )


def _bad_request(detail: str) -> HTTPException:
    """Build a 400 with a caller-actionable message."""
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


@router.post(
    '/analyze/bulk',
    response_model=BulkAnalyzeResponse,
    summary='Analyse several symbols on one timeframe with one strategy',
    responses={
        400: {
            'model': ErrorResponse,
            'description': 'Bad market, timeframe, strategy, or symbol list',
        },
        503: {'model': ErrorResponse, 'description': 'Market data unavailable'},
    },
)
def analyze_bulk(
    payload: BulkAnalyzeRequest,
    pipeline: SignalPipeline = Depends(get_pipeline),
    settings: Settings = Depends(get_settings),
) -> BulkAnalyzeResponse:
    """Run the production analysis for every requested symbol.

    Each symbol takes the identical path a single ``POST /api/analyze`` takes,
    and its complete response is embedded in the result untouched — so the scan
    and the analysis page can never disagree about the same setup.

    A symbol that fails is reported as a failed RESULT with a safe reason, not
    as an error: an unlisted coin or a venue hiccup on one pair must not cost
    the caller the other forty-nine analyses. The request itself fails only when
    the batch could not be run at all — a malformed request (400) or a provider
    that could not be reached (503).

    Args:
        payload: The validated request body.
        pipeline: The shared signal pipeline.
        settings: Application settings, for provider resolution.

    Returns:
        One :class:`BulkResultModel` per de-duplicated symbol, in request order,
        alongside the batch counters.

    Raises:
        HTTPException: 400 for a rejected batch, 503 when the venue is
            unreachable.
    """
    # ── Market ────────────────────────────────────────────────────────────────
    # Enforced here and not only in the caller: this route is reachable by
    # anything holding the service credential, so the restriction has to live
    # where the work actually happens. Note its exact strength — BULK_BLOCKED_MARKETS
    # is a DENYLIST, so this refuses forex and nothing else; any other registered
    # market passes. The "crypto only" product promise is the Next.js layer's to
    # keep. See the constant's own comment for why an allowlist is not used.
    market = (payload.market or '').strip().lower()
    if market in BULK_BLOCKED_MARKETS:
        raise _bad_request(
            f'Bulk analysis is not available for the {market} market.'
        )

    # ── Timeframe ─────────────────────────────────────────────────────────────
    try:
        timeframe = normalize_timeframe(payload.timeframe)
    except UnsupportedTimeframeError as exc:
        raise _bad_request(str(exc)) from exc

    if timeframe not in TRADEABLE_TIMEFRAMES:
        raise _bad_request(
            f'Timeframe {timeframe!r} is not selectable. '
            f'Choose one of: {", ".join(TRADEABLE_TIMEFRAMES)}.'
        )

    # ── Strategy ──────────────────────────────────────────────────────────────
    # Resolved from the registry, never substituted. Unlike the single route
    # there is no fallback at all: see the module docstring for why a batch
    # caller that named no strategy is a bug rather than a legacy client.
    allowed = ', '.join(_production_strategy_ids())
    declared = (payload.strategy_id or '').strip().upper()

    if not declared or declared == CUSTOM_STRATEGY_ID:
        raise _bad_request(
            'strategy_id is required for bulk analysis and has no default. '
            f'Bulk analysis runs production strategies only. Choose one of: {allowed}.'
        )

    strategy = get_strategy(declared)
    if strategy is None:
        raise _bad_request(
            f'Unknown strategy_id {payload.strategy_id!r}. '
            f'Bulk analysis runs production strategies only. Choose one of: {allowed}.'
        )
    if strategy.status != StrategyStatus.PRODUCTION:
        raise _bad_request(
            f'Strategy {strategy.strategy_id} is {strategy.status}. '
            f'Bulk analysis runs production strategies only. Choose one of: {allowed}.'
        )

    # ── Symbols ───────────────────────────────────────────────────────────────
    # Two different kinds of "bad symbol", handled two different ways:
    #
    #   MALFORMED (normalize_symbol raises) is a CLIENT error and fails the
    #   whole request with a 400. The caller sent something that is not a symbol
    #   at all; running the rest of the batch would hide the bug.
    #
    #   WELL-FORMED BUT UNLISTED at the venue is a RUNTIME condition and becomes
    #   a per-symbol 'failed' result. Whether a venue lists a given pair is not
    #   something the caller can know in advance, and one delisted coin must not
    #   cost the other forty-nine analyses.
    symbols: List[str] = []
    seen = set()
    for raw in payload.symbols:
        try:
            canonical = normalize_symbol(raw)
        except UnknownSymbolError as exc:
            raise _bad_request(str(exc)) from exc
        if canonical not in seen:
            seen.add(canonical)
            symbols.append(canonical)

    # The ceiling is applied AFTER de-duplication, so eleven copies of one pair
    # are one symbol and run. This is the limit a caller can act on; the schema's
    # `max_length` on `symbols` is a separate, much larger outer bound whose only
    # job is to stop an absurd list from being walked by the loop above.
    if not symbols or len(symbols) > MAX_BULK_SYMBOLS_PER_REQUEST:
        raise _bad_request(
            f'Bulk analysis accepts 1 to {MAX_BULK_SYMBOLS_PER_REQUEST} symbols '
            f'per request; received {len(symbols)} after de-duplication.'
        )

    # ── Provider ──────────────────────────────────────────────────────────────
    # Resolved once and shared by every symbol, exactly as the single route
    # resolves it. Same failure mapping, so a venue outage reads identically on
    # both routes.
    try:
        provider = get_provider(settings, payload.market, payload.provider)
    except UnknownProviderError as exc:
        raise _bad_request(str(exc)) from exc
    except Exception as exc:
        logger.exception('Provider init failed for market=%s', payload.market)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f'Market data provider unavailable: {exc}',
        ) from exc

    outcome = run_bulk(
        pipeline, provider, symbols, timeframe, strategy,
        serializer=to_analyze_response,
    )

    return BulkAnalyzeResponse(
        market=provider.market,
        timeframe=timeframe,
        strategy_id=strategy.strategy_id,
        strategy_status=strategy.status,
        provider=provider.name,
        requested=len(outcome.results),
        completed=outcome.completed,
        failed=outcome.failed,
        concurrency=outcome.concurrency,
        elapsed_ms=outcome.elapsed_ms,
        results=[
            BulkResultModel(
                symbol=item.symbol,
                status=item.status,
                # A reason is meaningful only on a failure; a completed result
                # carries its explanation inside its own analysis payload.
                reason=None if item.status == STATUS_COMPLETED else item.reason,
                analysis=item.analysis,
            )
            for item in outcome.results
        ],
    )
