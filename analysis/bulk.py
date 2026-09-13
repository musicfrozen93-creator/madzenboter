"""Bulk execution — run the EXISTING analysis for many symbols in one request.

WHAT THIS IS, AND WHAT IT IS NOT
    This module is a multi-symbol EXECUTION layer around ``SignalPipeline.run``.
    It is not a second signal engine and it performs no analysis of any kind:
    no scoring, no thresholds, no decision logic, no level derivation. Every
    symbol takes exactly the path a single ``POST /api/analyze`` takes —
    ``pipeline.run(...)`` followed by the same serializer — and the full,
    unmodified single-analysis payload is what this module hands back.

    That is deliberate. If bulk re-derived even one field (a direction, a
    quality score, an entry) the two paths could drift apart, and a user would
    read one verdict in the scan and a different one on the analysis page. By
    carrying the entire response through untouched, divergence is structurally
    impossible rather than merely discouraged.

WHY A THREAD POOL AND NOT ASYNC
    The pipeline and the provider layer are synchronous, and the wall time of
    one analysis is dominated by venue round-trips, not by arithmetic. A
    bounded thread pool overlaps those round-trips without touching a line of
    engine code; converting the pipeline to async would mean rewriting the
    thing this feature is explicitly forbidden to modify.

RATE LIMITING — the existing controls, deliberately not duplicated
    Nothing here adds a retry, a backoff, or a token bucket. Three controls
    already sit under this module and they compose:

      * ``exchange/client.py`` throttles client-side through ccxt's
        ``enableRateLimit``, which paces outbound calls to the venue's declared
        limit, and retries transient failures with exponential backoff in
        ``ExchangeClient._retry`` (3 attempts, 1s/2s/4s).
      * ``analysis/cache.py`` holds a process-wide ``CandleCache`` with a TTL
        scaled to the bar interval, plus a per-request ``RequestScope`` memo.
        Across a batch this matters: the shared benchmark and any repeated
        timeframe are fetched once per TTL window and read from cache
        thereafter. Note the limit precisely — ``CandleCache.get_or_fetch``
        runs its fetch OUTSIDE its lock, deliberately, so symbols that miss the
        same key SIMULTANEOUSLY all fetch it. The first scheduling wave of a
        cold batch therefore costs up to ``workers`` benchmark fetches, not
        one; every wave after it reads from cache. That is a bounded, one-off
        cost, not the per-symbol saving the cache is there for.
      * BOUNDED CONCURRENCY here is the primary control — the number of
        analyses in flight can never exceed ``resolved_concurrency()``.

    A second retry layer would be actively harmful. Retries stack
    MULTIPLICATIVELY with the existing one: 3 attempts here around 3 attempts
    there is 9 requests for one candle fetch, which is precisely the load
    amplification that turns a venue hiccup into an outage. A symbol that fails
    fails once, is reported as failed, and the batch moves on.

CHUNK SIZE, CONCURRENCY, AND WAVES — one number depends on the other
    Symbols queue behind the worker bound, so a batch runs in
    ``ceil(len(symbols) / workers)`` SCHEDULING WAVES, and the batch deadline is
    the per-symbol budget times that wave count. The caller's chunk size is
    therefore not a free parameter:

        Next.js ``BULK_CHUNK_SIZE`` (4)  ==  ``MAX_CONCURRENT_ANALYSES`` (4)
            =>  ceil(4 / 4) = 1 wave  =>  deadline = 1 x budget (25s)

    That equality is the whole point of the shipped numbers. One chunk is
    exactly ONE wave, which is what lets the caller's HTTP timeout and its
    serverless function budget be sized against a single, predictable figure
    instead of a multiple nobody recomputed.

    RAISING EITHER NUMBER COSTS SOMETHING SPECIFIC, so raise them together or
    not at all:

      * Chunk size above concurrency adds a wave. At chunk 5 / concurrency 4 the
        deadline doubles to 2 x 25s = 50s for one extra symbol — a 25s jump in
        the worst-case response time of every chunk, for a 25% throughput gain.
      * Concurrency above 4 pushes more simultaneous calls through the SHARED
        ccxt client, which is the caveat immediately below and the thing the
        clamp of 8 exists to bound.

    If the chunk size ever moves, ``MAX_CONCURRENT_ANALYSES`` must move with it,
    and the caller's engine timeout must be re-derived from the new wave count.

A KNOWN CHARACTERISTIC: A BATCH SHARES ONE BENCHMARK FETCH
    Every analysis pulls the market's systemic benchmark for broad-market
    context — ``BTCUSDT`` for crypto (``providers/base.reference_symbol``). It
    is ONE symbol that EVERY row in the batch depends on, which makes it the
    one venue call whose latency is not per-row:

      * A benchmark FAILURE is already harmless. ``AnalysisEngine._safe_benchmark``
        catches it, logs it, and continues with ``BtcRegime.UNKNOWN``, so the
        rows still produce analyses — with systemic context missing rather than
        with a failed batch.
      * A benchmark STALL is the sharp edge. A slow BTC fetch is charged to
        every worker that misses the cache, so a venue stall on that one pair
        degrades the WHOLE batch's wall time rather than one row's, and can
        push rows into the deadline as timeouts.

    This is stated, not fixed: the benchmark lives in the analysis engine, and
    changing what an analysis fetches would change what an analysis MEANS. The
    batch stays bounded and correct either way — the deadline still fires, every
    symbol still gets a result — so the cost of a BTC stall is latency and a
    crop of ``REASON_TIMEOUT`` rows, never a wrong number.

THE ONE CAVEAT WE ARE NOT FIXING
    The ccxt sync client instance is SHARED across these worker threads. The
    provider registry caches one initialized provider per venue for the process
    lifetime (``providers/registry.get_provider``), and the threads in this pool
    all call into that single object. ccxt's sync client is not documented as
    thread-safe, and its internal rate-limit bookkeeping is a plain timestamp,
    so concurrent callers can interleave in ways the throttle does not perfectly
    account for.

    Why this is safe in practice at the shipped default: concurrency defaults to
    4 and is hard-clamped to 8, so at most a handful of requests are ever in
    flight; the client is READ-ONLY (no order, balance, or position call exists,
    so there is no mutable session state worth corrupting); each call builds its
    own request and returns its own frame; and the candle cache removes most of
    the repeated traffic a batch would otherwise generate.

    Raising ``BULK_MAX_CONCURRENCY`` is the knob that makes this unsafe — it is
    the setting that pushes the shared throttle past what it can pace and
    invites venue-side 429s. The fix, if a larger bound is ever needed, is a
    per-thread or pooled exchange client in the provider layer, not a bigger
    number here. Changing that layer is out of scope for this feature.
"""

from __future__ import annotations

import logging
import math
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, TYPE_CHECKING

from analysis.pipeline import AnalysisResult, SignalPipeline
from analysis.strategies import Strategy
from providers.base import (
    MarketDataProvider,
    MarketDataUnavailableError,
    ProviderError,
    UnknownSymbolError,
    UnsupportedTimeframeError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from api.schemas import AnalyzeResponse

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Bounds
# ─────────────────────────────────────────────
# Every bound below is env-overridable and CLAMPED. Clamping rather than
# trusting the value matters because these are read from the deployment
# environment: a typo of "40" for "4" must not be able to point forty
# simultaneous analyses at one exchange.

#: Markets bulk analysis refuses outright. Forex is excluded at the product
#: level (the feature is sold as a crypto scan), and refusing it here means the
#: engine enforces it even if a caller bypasses the Next.js layer.
#:
#: BE PRECISE ABOUT WHAT THIS IS: a DENYLIST, not an allowlist. It guarantees
#: that ``forex`` can never be scanned. It does NOT guarantee that only crypto
#: can be — any market registered in the provider registry that is not named
#: here is permitted by default, so a future third market would be scannable the
#: moment it is registered, without anyone editing this line. That is a
#: deliberate trade, not an oversight: the test suite registers throwaway stub
#: markets (``tests/test_bulk_analyze.py`` uses ``'bulktest'``) and an allowlist
#: of ``{'crypto'}`` would refuse every one of them, which would mean the tests
#: could no longer exercise this route at all. The product-level guarantee that
#: only crypto is offered is enforced where the product lives, in the Next.js
#: layer; this set is the engine's backstop for the one market that must never
#: reach it. Adding a third real market means adding it here too.
BULK_BLOCKED_MARKETS: frozenset = frozenset({'forex'})

#: Hard ceiling on symbols in ONE engine request. The Next.js layer chunks a
#: 50-coin scan into requests of ``BULK_CHUNK_SIZE`` (4, chosen to equal
#: :data:`MAX_CONCURRENT_ANALYSES` so a chunk is exactly one scheduling wave —
#: see the module docstring), so this is a safety bound on a direct caller, not
#: the product limit a user sees and not the size a chunk actually arrives at.
MAX_BULK_SYMBOLS_PER_REQUEST: int = 10

_CONCURRENCY_ENV = 'BULK_MAX_CONCURRENCY'
# 4 is PAIRED with the Next.js BULK_CHUNK_SIZE, which is also 4: chunk size
# equal to concurrency means ceil(4 / 4) = ONE scheduling wave per chunk, so a
# chunk's deadline is one per-symbol budget rather than a multiple of it.
# Changing this alone silently re-times every chunk the caller sends — move
# BULK_CHUNK_SIZE with it. See the module docstring for the full arithmetic.
_CONCURRENCY_DEFAULT = 4
_CONCURRENCY_MIN = 1
_CONCURRENCY_MAX = 8

_TIMEOUT_ENV = 'BULK_SYMBOL_TIMEOUT_S'
_TIMEOUT_DEFAULT = 25
_TIMEOUT_MIN = 5
_TIMEOUT_MAX = 120


# ─────────────────────────────────────────────
# Failure reasons
# ─────────────────────────────────────────────
# These six strings are the ONLY thing a caller ever learns about a failed
# symbol. An exception message or a traceback is never propagated: it can carry
# a venue URL, an internal path, or a library detail that tells an attacker
# about the engine's shape, and it is useless to the user either way. The
# specific exception is logged server-side instead.

REASON_UNKNOWN_SYMBOL = 'Symbol not available'
REASON_UNSUPPORTED_TIMEFRAME = 'Timeframe not supported'
REASON_NO_HISTORY = 'Not enough price history'
REASON_DATA_UNAVAILABLE = 'Market data unavailable'
REASON_TIMEOUT = 'Analysis timed out'
REASON_FAILED = 'Analysis failed'

#: Per-symbol outcome states. A batch NEVER has any other status: a symbol
#: either produced a full analysis or it did not.
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'


def _clamped_env_int(name: str, default: int, low: int, high: int) -> int:
    """Read an integer from the environment and clamp it into a safe range.

    A missing, blank, or unparseable value falls back to ``default`` rather than
    raising: a bad env var must degrade to the shipped setting, not stop the
    service from starting.

    Args:
        name: Environment variable to read.
        default: Value used when the variable is absent or unparseable.
        low: Inclusive lower bound.
        high: Inclusive upper bound.

    Returns:
        The clamped integer.
    """
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning('%s=%r is not an integer; using %d', name, raw, default)
        return default
    clamped = max(low, min(high, value))
    if clamped != value:
        logger.warning(
            '%s=%d is outside the permitted range %d-%d; clamped to %d',
            name, value, low, high, clamped,
        )
    return clamped


def resolved_concurrency() -> int:
    """How many analyses may run at once, clamped to a safe bound.

    Pure with respect to the process environment, so a test can assert the
    bound is actually enforced without constructing a pool.

    Returns:
        An integer in ``[1, 8]``.
    """
    return _clamped_env_int(
        _CONCURRENCY_ENV, _CONCURRENCY_DEFAULT, _CONCURRENCY_MIN, _CONCURRENCY_MAX,
    )


def resolved_symbol_timeout() -> int:
    """The per-symbol analysis budget in seconds, clamped to a safe bound.

    Returns:
        An integer in ``[5, 120]``.
    """
    return _clamped_env_int(
        _TIMEOUT_ENV, _TIMEOUT_DEFAULT, _TIMEOUT_MIN, _TIMEOUT_MAX,
    )


#: Import-time snapshots of the resolved bounds, for logging and introspection.
#: :func:`run_bulk` calls the resolver functions per run, so changing the
#: environment takes effect without reimporting the module.
#:
#: :data:`MAX_CONCURRENT_ANALYSES` is the number the caller's chunk size is
#: sized against: Next.js ships ``BULK_CHUNK_SIZE = 4`` precisely BECAUSE this
#: resolves to 4 by default, so one chunk is one wave and one chunk's deadline
#: is one 25s budget. Raising ``BULK_MAX_CONCURRENCY`` in the environment
#: without raising the chunk size wastes workers; raising the chunk size without
#: raising this adds a wave and doubles the chunk deadline.
MAX_CONCURRENT_ANALYSES: int = resolved_concurrency()
SYMBOL_TIMEOUT_SECONDS: int = resolved_symbol_timeout()


# ─────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class BulkSymbolResult:
    """One symbol's outcome.

    ``analysis`` is the COMPLETE single-analysis response object, passed through
    without inspection. Nothing in this module reads a field of it.
    """

    symbol: str
    status: str
    reason: Optional[str] = None
    analysis: Optional['AnalyzeResponse'] = None


@dataclass(frozen=True)
class BulkRunOutcome:
    """Everything one bulk execution produced.

    ``results`` preserves the order of the symbols that were handed in, so a
    caller can zip it against its own request without matching on symbol.
    """

    results: List[BulkSymbolResult]
    completed: int
    failed: int
    concurrency: int
    elapsed_ms: int


def failure_reason(exc: BaseException) -> str:
    """Map an exception to the one safe sentence the caller is allowed to see.

    Ordered most specific first: ``UnknownSymbolError``,
    ``UnsupportedTimeframeError`` and ``MarketDataUnavailableError`` are all
    ``ProviderError`` subclasses, so the generic provider branch has to come
    last among them.

    Args:
        exc: The exception raised while analysing one symbol.

    Returns:
        A fixed, user-safe reason string. Never the exception's own message.
    """
    if isinstance(exc, UnknownSymbolError):
        return REASON_UNKNOWN_SYMBOL
    if isinstance(exc, UnsupportedTimeframeError):
        return REASON_UNSUPPORTED_TIMEFRAME
    if isinstance(exc, (MarketDataUnavailableError, ProviderError)):
        return REASON_DATA_UNAVAILABLE
    if isinstance(exc, TimeoutError):
        return REASON_TIMEOUT
    if isinstance(exc, ValueError):
        # The pipeline raises a bare ValueError when the selected timeframe has
        # too few candles for the enabled module set — a young listing, or a
        # venue that only keeps a short history.
        return REASON_NO_HISTORY
    return REASON_FAILED


def _default_serializer() -> Callable[[AnalysisResult], 'AnalyzeResponse']:
    """Resolve the single-analysis serializer, imported lazily.

    ``api.serializers`` is imported inside the function rather than at module
    scope on purpose. ``api/__init__.py`` builds the FastAPI app, which imports
    the route that imports THIS module; a top-level ``from api.serializers
    import ...`` here would therefore make ``import analysis.bulk`` circular.
    The lazy import also keeps the ``analysis`` package free of a hard
    dependency on the API layer, which is the boundary the rest of the engine
    maintains.

    Returns:
        ``api.serializers.to_analyze_response``.
    """
    from api.serializers import to_analyze_response

    return to_analyze_response


def _analyze_one(
    pipeline: SignalPipeline,
    provider: MarketDataProvider,
    symbol: str,
    timeframe: str,
    strategy: Strategy,
    serializer: Callable[[AnalysisResult], 'AnalyzeResponse'],
) -> 'AnalyzeResponse':
    """Run the production single-symbol path for one symbol.

    This is the whole of the per-symbol work, and it is intentionally four
    lines: the same ``pipeline.run`` call the single route makes, with the same
    arguments, serialized by the same function.

    Args:
        pipeline: The shared, stateless signal pipeline.
        provider: An initialized market-data provider.
        symbol: A normalized platform symbol.
        timeframe: A normalized, tradeable timeframe.
        strategy: The resolved strategy definition to run.
        serializer: Converts an ``AnalysisResult`` into the response model.

    Returns:
        The full single-analysis response for this symbol.

    Raises:
        Exception: Whatever the pipeline raises. Mapping it to a safe reason is
            the caller's job, in :func:`failure_reason`.
    """
    result = pipeline.run(
        provider, symbol, timeframe,
        enabled_modules=strategy.modules,
        # What the caller SAID it was running — consulted only to make the
        # verdict more restrictive, exactly as on the single route.
        declared_strategy_id=strategy.strategy_id,
    )
    return serializer(result)


def run_bulk(
    pipeline: SignalPipeline,
    provider: MarketDataProvider,
    symbols: Sequence[str],
    timeframe: str,
    strategy: Strategy,
    *,
    serializer: Optional[Callable[[AnalysisResult], 'AnalyzeResponse']] = None,
    concurrency: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
) -> BulkRunOutcome:
    """Analyse several symbols concurrently and return one outcome per symbol.

    Provider-agnostic by construction: the pipeline and the provider are
    arguments, so the executor runs unchanged against a stub in tests and
    against a live venue in production.

    ONE FAILED SYMBOL NEVER FAILS THE BATCH. Every exception is caught, mapped
    to a safe reason and recorded against that symbol; the remaining symbols
    keep running. A bulk request either returns results for everything it was
    asked about or it does not return at all.

    THE DEADLINE. Each symbol gets ``timeout_seconds`` of budget, but symbols
    queue behind the concurrency bound, so the batch deadline is that budget
    times the number of scheduling waves (``ceil(symbols / concurrency)``) — a
    symbol is not charged for the time it spent waiting its turn. At the shipped
    numbers the caller sends exactly ``concurrency`` symbols per chunk, so
    ``waves`` is 1 and the deadline is one budget; that equality is what the
    caller's own HTTP timeout is sized against. Futures still pending when the
    deadline passes are cancelled and reported as timed out, and the pool is
    torn down without waiting, so one hung venue call can delay a request by at
    most the deadline instead of pinning it open forever.

    A thread already executing a hung call cannot be interrupted by
    ``cancel()`` — Python has no safe thread kill — so it keeps running in the
    background until the underlying venue call gives up. That is acceptable
    because it no longer blocks the response and the worker count is bounded,
    but be honest about how long "until it gives up" is: the ccxt client carries
    its default 10s socket timeout and ``ExchangeClient._retry`` makes three
    attempts with 1s/2s/4s backoff, so a single hung fetch can run ~37s, and an
    analysis makes several fetches. An abandoned worker can therefore outlive
    the response it was dropped from by a minute or two, and its threads are
    non-daemon, so a process shutdown waits for them. Bounded and self-clearing,
    but real: capping it tightly would mean setting an explicit timeout on the
    shared exchange client, which is a provider-layer change and out of scope
    for this module.

    Args:
        pipeline: The shared signal pipeline.
        provider: An initialized market-data provider for the requested market.
        symbols: Normalized, de-duplicated platform symbols, in request order.
        timeframe: A normalized, tradeable timeframe.
        strategy: The resolved strategy definition. Its ``modules`` are what run
            and its ``strategy_id`` is what is declared to the pipeline.
        serializer: Response serializer. Defaults to the single-analysis
            ``to_analyze_response``; injectable so a test can avoid the API
            layer entirely.
        concurrency: Override the resolved worker bound. Still clamped.
        timeout_seconds: Override the per-symbol budget. Still clamped.

    Returns:
        A :class:`BulkRunOutcome` whose ``results`` are in the same order as
        ``symbols``.
    """
    started = time.perf_counter()
    serialize = serializer or _default_serializer()

    workers = resolved_concurrency() if concurrency is None else max(
        _CONCURRENCY_MIN, min(_CONCURRENCY_MAX, int(concurrency))
    )
    budget = resolved_symbol_timeout() if timeout_seconds is None else max(
        _TIMEOUT_MIN, min(_TIMEOUT_MAX, int(timeout_seconds))
    )

    ordered = list(symbols)
    if not ordered:
        return BulkRunOutcome(
            results=[], completed=0, failed=0, concurrency=workers, elapsed_ms=0,
        )

    # Symbols queue behind `workers`, so the batch runs in this many scheduling
    # waves and the deadline is the per-symbol budget times that count. The
    # caller's chunk size is deliberately EQUAL to the default concurrency
    # (Next.js BULK_CHUNK_SIZE 4 == MAX_CONCURRENT_ANALYSES 4), which makes this
    # exactly 1 in production and the deadline exactly one budget. A chunk one
    # symbol larger than `workers` makes it 2 and doubles the deadline.
    waves = math.ceil(len(ordered) / workers)
    batch_deadline_seconds = float(budget * waves)
    deadline = time.monotonic() + batch_deadline_seconds

    results: List[Optional[BulkSymbolResult]] = [None] * len(ordered)

    # Not a `with` block: ThreadPoolExecutor.__exit__ calls shutdown(wait=True),
    # which would block on exactly the hung call the deadline exists to escape.
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='bulk-analysis')
    try:
        futures: Dict[int, Future] = {
            index: pool.submit(
                _analyze_one, pipeline, provider, symbol, timeframe,
                strategy, serialize,
            )
            for index, symbol in enumerate(ordered)
        }

        for index, symbol in enumerate(ordered):
            future = futures[index]
            remaining = max(0.0, deadline - time.monotonic())
            try:
                payload = future.result(timeout=remaining)
            except FutureTimeoutError as exc:
                # This branch catches TWO different events, because on Python
                # 3.11+ concurrent.futures.TimeoutError IS the builtin
                # TimeoutError: our own wait on the batch deadline expiring, and
                # a venue socket timeout raised INSIDE the pipeline. Both are
                # REASON_TIMEOUT to the caller and always will be — they differ
                # only in what an operator should DO, so they differ only here.
                # A finished future means the work ended by raising, not that
                # our wait ran out.
                if future.done():
                    # `exc` here is the pipeline's OWN exception: Future.result
                    # re-raises what the callable raised rather than wrapping
                    # it. %r rather than %s because a socket TimeoutError often
                    # carries no message at all, and a bare blank tells an
                    # operator nothing.
                    logger.warning(
                        'Bulk analysis timed out for %s %s inside the '
                        'pipeline (a venue call, not the batch deadline): %r',
                        symbol, timeframe, exc,
                    )
                else:
                    future.cancel()
                    # Log the deadline that ACTUALLY expired, plus the
                    # arithmetic behind it. Logging the per-symbol budget alone
                    # is actively misleading: an operator sizing
                    # BULK_SYMBOL_TIMEOUT_S from this line would otherwise be
                    # reading a number `waves` times smaller than the one that
                    # fired.
                    logger.warning(
                        'Bulk analysis timed out for %s %s: the %.1fs batch '
                        'deadline expired (%.1fs per-symbol budget x %d wave(s) '
                        'of %d symbols at concurrency %d)',
                        symbol, timeframe, batch_deadline_seconds, float(budget),
                        waves, len(ordered), workers,
                    )
                results[index] = BulkSymbolResult(
                    symbol=symbol, status=STATUS_FAILED, reason=REASON_TIMEOUT,
                )
            except (
                UnknownSymbolError, UnsupportedTimeframeError, ProviderError,
                ValueError, TimeoutError,
            ) as exc:
                # Expected, well-understood failures. Logged at warning with the
                # real message for operators; the caller sees only the mapping.
                # `TimeoutError` is listed for Python < 3.11 only; from 3.11 it
                # is the same class as concurrent.futures.TimeoutError and is
                # therefore already handled by the branch above. Keep it: the
                # mapping must not depend on the interpreter version.
                reason = failure_reason(exc)
                logger.warning(
                    'Bulk analysis failed for %s %s: %s (%s)',
                    symbol, timeframe, reason, exc,
                )
                results[index] = BulkSymbolResult(
                    symbol=symbol, status=STATUS_FAILED, reason=reason,
                )
            except Exception as exc:  # noqa: BLE001 - one symbol must not sink the batch
                # Genuinely unexpected: a bug, not a market condition. Full
                # traceback server-side, generic reason to the caller.
                logger.exception('Bulk analysis errored for %s %s', symbol, timeframe)
                results[index] = BulkSymbolResult(
                    symbol=symbol, status=STATUS_FAILED, reason=failure_reason(exc),
                )
            else:
                results[index] = BulkSymbolResult(
                    symbol=symbol, status=STATUS_COMPLETED, analysis=payload,
                )
    finally:
        # cancel_futures drops everything still queued; wait=False means a
        # thread stuck in a venue call cannot hold the response open.
        pool.shutdown(wait=False, cancel_futures=True)

    final: List[BulkSymbolResult] = [r for r in results if r is not None]
    completed = sum(1 for r in final if r.status == STATUS_COMPLETED)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    logger.info(
        'Bulk analysis: %d symbols, %d completed, %d failed, %s %s, '
        'concurrency=%d, %dms',
        len(final), completed, len(final) - completed, timeframe,
        strategy.strategy_id, workers, elapsed_ms,
    )

    return BulkRunOutcome(
        results=final,
        completed=completed,
        failed=len(final) - completed,
        concurrency=workers,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    'BULK_BLOCKED_MARKETS',
    'MAX_BULK_SYMBOLS_PER_REQUEST',
    'MAX_CONCURRENT_ANALYSES',
    'SYMBOL_TIMEOUT_SECONDS',
    'REASON_DATA_UNAVAILABLE',
    'REASON_FAILED',
    'REASON_NO_HISTORY',
    'REASON_TIMEOUT',
    'REASON_UNKNOWN_SYMBOL',
    'REASON_UNSUPPORTED_TIMEFRAME',
    'STATUS_COMPLETED',
    'STATUS_FAILED',
    'BulkRunOutcome',
    'BulkSymbolResult',
    'failure_reason',
    'resolved_concurrency',
    'resolved_symbol_timeout',
    'run_bulk',
]
