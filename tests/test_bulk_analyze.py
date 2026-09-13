"""POST /api/analyze/bulk — the multi-symbol EXECUTION layer, over real HTTP.

WHAT THESE TESTS PIN
    Bulk analysis is not a second signal engine. It runs the production
    single-symbol path once per symbol and hands the untouched payload back, so
    the scan and the analysis page can never disagree about the same setup. The
    central test in this file (`test_bulk_matches_the_single_analyze_route`)
    asserts exactly that, field by field.

    Everything else guards the batch boundary: the validation a batch caller
    gets that a single caller does not (no default strategy, production only, a
    hard symbol ceiling, crypto only), the isolation guarantee (one bad symbol
    costs its own result and nothing else), the safety of what a failure is
    allowed to say, and the bound on how much venue traffic one request can
    generate.

HOW THEY RUN
    No network and no database. A `StubProvider` subclass is registered for a
    throwaway market, and failures, hangs and latency are injected through it —
    which is the only honest way to test the executor, since the executor's
    whole job is to survive what the provider does.

    The registry constructs and caches ONE provider instance per process and
    the test never holds it, so every control and counter below lives on the
    CLASS and the fixture resets it between tests.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Set

import pytest
from fastapi.testclient import TestClient

from analysis import bulk
from analysis.bulk import (
    BULK_BLOCKED_MARKETS,
    MAX_BULK_SYMBOLS_PER_REQUEST,
    MAX_CONCURRENT_ANALYSES,
    REASON_DATA_UNAVAILABLE,
    REASON_FAILED,
    REASON_NO_HISTORY,
    REASON_TIMEOUT,
    REASON_UNKNOWN_SYMBOL,
    REASON_UNSUPPORTED_TIMEFRAME,
    STATUS_COMPLETED,
    STATUS_FAILED,
    failure_reason,
    resolved_concurrency,
    resolved_symbol_timeout,
)
from analysis.strategies import (
    CUSTOM_STRATEGY_ID,
    DEFAULT_STRATEGY_ID,
    STRATEGY_ORDER,
    StrategyStatus,
)
from api.app import create_app
from api.dependencies import get_settings
from providers.base import (
    TIMEFRAMES,
    TRADEABLE_TIMEFRAMES,
    MarketDataUnavailableError,
    UnknownSymbolError,
    UnsupportedTimeframeError,
)
from providers.registry import (
    _DEFAULT_FOR_MARKET,
    _REGISTRY,
    register,
    reset_for_tests,
)
from tests.conftest import TEST_SERVICE_KEY
from tests.fakes import StubProvider, make_uptrend

TEST_MARKET = 'bulktest'
BULK_URL = '/api/analyze/bulk'

#: The one production strategy. Read from the registry rather than hardcoded so
#: promoting a second strategy does not silently invalidate these tests.
PRODUCTION_ID = DEFAULT_STRATEGY_ID

EXPERIMENTAL_IDS = [
    s.strategy_id for s in STRATEGY_ORDER if s.status == StrategyStatus.EXPERIMENTAL
]

#: Exactly MAX_BULK_SYMBOLS_PER_REQUEST symbols, so the ceiling itself can be
#: exercised against a provider that actually lists them.
LISTED_SYMBOLS = (
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT',
    'DOGEUSDT', 'LINKUSDT', 'AVAXUSDT', 'DOTUSDT', 'ATOMUSDT',
)

#: Well-formed, but the venue does not list it — a failed RESULT, not a 400.
UNLISTED_SYMBOL = 'LTCUSDT'

#: The complete vocabulary a failed symbol is allowed to report.
SAFE_REASONS = frozenset({
    REASON_UNKNOWN_SYMBOL,
    REASON_UNSUPPORTED_TIMEFRAME,
    REASON_NO_HISTORY,
    REASON_DATA_UNAVAILABLE,
    REASON_TIMEOUT,
    REASON_FAILED,
})

#: An exception message carrying everything a reason must never repeat back:
#: a venue URL with a credential, an internal file path, and a traceback.
POISON = (
    'https://api.venue.example/v3/klines?symbol=X&apiKey=SECRET failed in '
    '/srv/app/providers/binance.py line 118\n'
    'Traceback (most recent call last):\n'
    '  File "/srv/app/analysis/pipeline.py", line 42, in run'
)

#: Substrings that would prove an internal detail escaped into a reason.
LEAK_MARKERS = (
    'http', '://', '/', '\\', '.py', 'Traceback', 'File "', 'SECRET',
    'venue.example', 'line 118',
)

#: Upper bound on how long an injected hang blocks its worker thread. The test
#: releases it as soon as it has asserted, but a ceiling guarantees a stuck
#: worker can never outlive the suite (ThreadPoolExecutor threads are joined at
#: interpreter exit).
HANG_CEILING_SECONDS = 20.0


# ─────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────

class BulkStubProvider(StubProvider):
    """A registered stub whose behaviour each test drives from the class."""

    name = 'bulkstub'
    market = TEST_MARKET

    # ── Injected behaviour ──
    #: symbol -> zero-arg factory returning the exception to raise.
    failures: Dict[str, Callable[[], BaseException]] = {}
    #: symbols whose fetch blocks as if the venue never answered.
    hanging: Set[str] = set()
    #: Released by the test (and by the fixture) so a hung worker always exits.
    release = threading.Event()
    #: Artificial per-fetch latency, used to make overlap observable.
    fetch_delay = 0.0

    # ── Observation ──
    _counter_lock = threading.Lock()
    in_flight = 0
    peak_in_flight = 0
    worker_threads: Set[str] = set()

    def __init__(self, settings=None):
        # The registry constructs providers with settings; the stub ignores them.
        super().__init__(candles=make_uptrend(n=500), symbols=LISTED_SYMBOLS)

    @classmethod
    def reset(cls) -> None:
        """Drop every injected behaviour and every counter."""
        cls.failures = {}
        cls.hanging = set()
        cls.release = threading.Event()
        cls.fetch_delay = 0.0
        cls.in_flight = 0
        cls.peak_in_flight = 0
        cls.worker_threads = set()

    def fetch_candles(self, symbol, timeframe, limit=300):
        """The single point every analysis must pass through.

        One analysis occupies exactly one worker thread, and a thread is inside
        at most one fetch at a time, so `in_flight` here IS the number of
        analyses simultaneously executing.
        """
        cls = type(self)
        with cls._counter_lock:
            cls.in_flight += 1
            cls.peak_in_flight = max(cls.peak_in_flight, cls.in_flight)
            cls.worker_threads.add(threading.current_thread().name)
        try:
            factory = cls.failures.get(symbol)
            if factory is not None:
                raise factory()
            if symbol in cls.hanging:
                cls.release.wait(timeout=HANG_CEILING_SECONDS)
            if cls.fetch_delay:
                time.sleep(cls.fetch_delay)
            return super().fetch_candles(symbol, timeframe, limit)
        finally:
            with cls._counter_lock:
                cls.in_flight -= 1


def _drain_workers(timeout: float = 15.0) -> None:
    """Wait for every thread the executor spawned to exit.

    `run_bulk` tears its pool down with `wait=False` ON PURPOSE, so a worker
    stuck in a hung venue call keeps running after the response is sent — that
    is the behaviour `test_a_hanging_provider_call_times_out_as_a_failed_symbol`
    exists to prove. Left to itself, such a thread finishes its analysis DURING
    the next test and writes its candles into the process-wide cache after the
    cache-clearing fixture has run, which silently turns the next test's
    injected failure into a cache hit.

    Draining here keeps that strictly a property of the executor and not a
    source of cross-test contamination.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [
            t for t in threading.enumerate()
            if t.name.startswith('bulk-analysis') and t.is_alive()
        ]
        if not alive:
            return
        for thread in alive:
            thread.join(timeout=0.05)


def _install(settings, *, authenticated: bool):
    """Build an app with the stub registered for the throwaway market."""
    _drain_workers()
    BulkStubProvider.reset()
    register(BulkStubProvider)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    headers = {'X-Service-Key': TEST_SERVICE_KEY} if authenticated else {}
    return app, TestClient(app, headers=headers)


def _teardown(app) -> None:
    app.dependency_overrides.clear()
    # Never leave a worker blocked on an injected hang, and never let an
    # abandoned one run on into the next test.
    BulkStubProvider.release.set()
    _drain_workers()
    BulkStubProvider.reset()
    _REGISTRY.pop(BulkStubProvider.name, None)
    _DEFAULT_FOR_MARKET.pop(TEST_MARKET, None)
    reset_for_tests()


@pytest.fixture
def client(settings):
    """A client presenting the service credential."""
    app, test_client = _install(settings, authenticated=True)
    with test_client as ready:
        yield ready
    _teardown(app)


@pytest.fixture
def raw_client(settings):
    """A client presenting NO credential — the bare curl an outsider runs."""
    app, test_client = _install(settings, authenticated=False)
    with test_client as ready:
        yield ready
    _teardown(app)


def _body(**overrides) -> dict:
    payload = {
        'market': TEST_MARKET,
        'symbols': ['BTCUSDT', 'ETHUSDT'],
        'timeframe': '15m',
        'strategy_id': PRODUCTION_ID,
    }
    payload.update(overrides)
    return payload


def _bulk(client, **overrides):
    return client.post(BULK_URL, json=_body(**overrides))


def _ok(client, **overrides) -> dict:
    res = _bulk(client, **overrides)
    assert res.status_code == 200, res.text
    return res.json()


def _rejected(client, **overrides) -> str:
    res = _bulk(client, **overrides)
    assert res.status_code == 400, f'expected 400, got {res.status_code}: {res.text}'
    return res.json()['detail']


def _single(client, symbol, **overrides) -> dict:
    payload = {
        'market': TEST_MARKET,
        'symbol': symbol,
        'timeframe': '15m',
        'strategy_id': PRODUCTION_ID,
    }
    payload.update(overrides)
    res = client.post('/api/analyze', json=payload)
    assert res.status_code == 200, res.text
    return res.json()


# ─────────────────────────────────────────────
# 1. The service boundary
# ─────────────────────────────────────────────

def test_bulk_rejects_a_request_with_no_service_credential(raw_client):
    """The batch route is behind the same credential as every analysis route.

    It is the most expensive endpoint in the service — one call is many venue
    conversations — so an unauthenticated caller reaching it would be worse than
    reaching /api/analyze, not better.
    """
    res = raw_client.post(BULK_URL, json=_body())
    assert res.status_code == 401
    assert 'credential' in res.json()['detail'].lower()


def test_bulk_rejects_a_forged_service_credential(raw_client):
    res = raw_client.post(
        BULK_URL, json=_body(), headers={'X-Service-Key': 'x' * 40},
    )
    assert res.status_code == 403


# ─────────────────────────────────────────────
# 2. Market entitlement — crypto only
# ─────────────────────────────────────────────

def test_bulk_rejects_the_forex_market(client):
    """Sold as a crypto scan, and enforced in the engine rather than only in the
    caller: this route is reachable by anything holding the service key."""
    detail = _rejected(client, market='forex')
    assert 'forex' in detail.lower()


@pytest.mark.parametrize('spelling', ['FOREX', 'Forex', '  forex  '])
def test_the_forex_block_is_not_case_or_whitespace_sensitive(client, spelling):
    """A blocked market must not be bypassable by how it is typed."""
    _rejected(client, market=spelling)


def test_forex_is_the_blocked_market_set(client):
    assert 'forex' in BULK_BLOCKED_MARKETS
    assert 'crypto' not in BULK_BLOCKED_MARKETS


# ─────────────────────────────────────────────
# 3-5. The symbol list
# ─────────────────────────────────────────────

def test_bulk_rejects_more_than_the_maximum_symbols(client):
    symbols = [f'SYM{i}USDT' for i in range(MAX_BULK_SYMBOLS_PER_REQUEST + 1)]
    detail = _rejected(client, symbols=symbols)
    assert str(MAX_BULK_SYMBOLS_PER_REQUEST) in detail


def test_bulk_accepts_exactly_the_maximum_symbols(client):
    """The ceiling is inclusive — the boundary itself must still run."""
    assert len(LISTED_SYMBOLS) == MAX_BULK_SYMBOLS_PER_REQUEST
    body = _ok(client, symbols=list(LISTED_SYMBOLS))
    assert body['requested'] == MAX_BULK_SYMBOLS_PER_REQUEST
    assert len(body['results']) == MAX_BULK_SYMBOLS_PER_REQUEST


def test_bulk_rejects_zero_symbols(client):
    detail = _rejected(client, symbols=[])
    assert '0' in detail


def test_bulk_rejects_a_missing_symbols_field(client):
    payload = _body()
    payload.pop('symbols')
    res = client.post(BULK_URL, json=payload)
    assert res.status_code == 400


@pytest.mark.parametrize('malformed', [
    '', '   ', 'BTC USDT', 'BTC*USDT', 'X', '../../etc/passwd',
    'BTCUSDT;DROP TABLE', '<script>', 'btc usdt',
])
def test_a_malformed_symbol_rejects_the_whole_request(client, malformed):
    """A malformed symbol is a CLIENT bug, not a market condition.

    Running the rest of the batch would hide it — so the request fails as a
    whole, even though every other symbol in it is valid.
    """
    detail = _rejected(client, symbols=['BTCUSDT', malformed, 'ETHUSDT'])
    assert 'symbol' in detail.lower()


def test_a_wellformed_but_unlisted_symbol_is_a_failed_result_not_a_400(client):
    """The caller cannot know what a venue lists, so this is a runtime outcome.

    One delisted coin must not cost the caller the rest of the batch.
    """
    body = _ok(client, symbols=['BTCUSDT', UNLISTED_SYMBOL])
    by_symbol = {r['symbol']: r for r in body['results']}
    assert by_symbol['BTCUSDT']['status'] == STATUS_COMPLETED
    assert by_symbol[UNLISTED_SYMBOL]['status'] == STATUS_FAILED
    assert by_symbol[UNLISTED_SYMBOL]['reason'] == REASON_UNKNOWN_SYMBOL
    assert by_symbol[UNLISTED_SYMBOL]['analysis'] is None


# ─────────────────────────────────────────────
# 6. Timeframes — the tradeable set only
# ─────────────────────────────────────────────

@pytest.mark.parametrize('timeframe', ['7m', '', '   ', 'banana', '15', '1H4'])
def test_bulk_rejects_a_timeframe_outside_the_vocabulary(client, timeframe):
    detail = _rejected(client, timeframe=timeframe)
    assert 'timeframe' in detail.lower()


@pytest.mark.parametrize('timeframe', ['1d', '1m', '5m', '1w', '12h'])
def test_bulk_rejects_a_known_but_untradeable_timeframe(client, timeframe):
    """'1d' is a real interval the engine reads internally — it is simply not
    one a user may select as an entry timeframe, on either route."""
    assert timeframe in TIMEFRAMES
    assert timeframe not in TRADEABLE_TIMEFRAMES
    detail = _rejected(client, timeframe=timeframe)
    assert 'not selectable' in detail.lower()


@pytest.mark.parametrize('timeframe', list(TRADEABLE_TIMEFRAMES))
def test_bulk_accepts_every_tradeable_timeframe(client, timeframe):
    body = _ok(client, symbols=['BTCUSDT'], timeframe=timeframe)
    assert body['timeframe'] == timeframe
    assert body['results'][0]['analysis']['timeframe'] == timeframe


# ─────────────────────────────────────────────
# 7-9. Strategy — production only, never defaulted
# ─────────────────────────────────────────────

@pytest.mark.parametrize('strategy_id', EXPERIMENTAL_IDS)
def test_bulk_rejects_every_experimental_strategy(client, strategy_id):
    """An experimental strategy is forced to WAIT by the decision layer, so a
    batch of them would spend the venue budget producing non-answers."""
    detail = _rejected(client, strategy_id=strategy_id)
    assert 'production' in detail.lower()
    assert PRODUCTION_ID in detail


@pytest.mark.parametrize('strategy_id', [CUSTOM_STRATEGY_ID, 'custom', ' Custom '])
def test_bulk_rejects_the_custom_strategy(client, strategy_id):
    """Bulk names a strategy and the backend decides what it contains. There is
    no path by which a batch caller assembles its own module set."""
    detail = _rejected(client, strategy_id=strategy_id)
    assert 'production' in detail.lower()


def test_bulk_rejects_an_unknown_strategy_id(client):
    detail = _rejected(client, strategy_id='ICT_MSNR_V99')
    assert 'production' in detail.lower()


@pytest.mark.parametrize('strategy_id', ['', '   ', None])
def test_bulk_rejects_a_missing_or_blank_strategy_id(client, strategy_id):
    """Bulk has NO default strategy. A batch caller is a program, and a program
    that forgot to name a strategy has a bug — running fifty analyses under a
    guess is the wrong way to find out."""
    payload = _body()
    if strategy_id is None:
        payload.pop('strategy_id')
    else:
        payload['strategy_id'] = strategy_id
    res = client.post(BULK_URL, json=payload)
    assert res.status_code == 400, res.text
    assert 'required' in res.json()['detail'].lower()


def test_the_omitted_strategy_id_never_falls_back_to_the_single_route_default(client):
    """The single route defaults an unconfigured request to the production
    strategy. Bulk must NOT inherit that convenience."""
    payload = _body()
    payload.pop('strategy_id')
    assert client.post(BULK_URL, json=payload).status_code == 400
    # The same omission on the single route is still served — unchanged.
    single = client.post(
        '/api/analyze', json={'market': TEST_MARKET, 'symbol': 'BTCUSDT',
                              'timeframe': '15m'},
    )
    assert single.status_code == 200
    assert single.json()['strategy_id'] == PRODUCTION_ID


def test_a_module_list_in_the_body_cannot_configure_a_bulk_scan(client):
    """`enabled_indicators` is not part of the bulk contract; the named
    strategy decides the module set and nothing else can."""
    body = _ok(client, symbols=['BTCUSDT'], enabled_indicators=['rsi'])
    analysis = body['results'][0]['analysis']
    assert analysis['strategy_id'] == PRODUCTION_ID
    assert len(analysis['enabled_indicators']) > 1


# ─────────────────────────────────────────────
# 10-11. The accepted request
# ─────────────────────────────────────────────

def test_the_production_strategy_returns_one_result_per_symbol(client):
    requested = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT']
    body = _ok(client, symbols=requested)

    assert [r['symbol'] for r in body['results']] == requested
    assert body['requested'] == len(requested)
    assert body['completed'] == len(requested)
    assert body['failed'] == 0
    assert body['market'] == TEST_MARKET
    assert body['provider'] == BulkStubProvider.name
    assert body['timeframe'] == '15m'
    assert body['strategy_id'] == PRODUCTION_ID
    assert body['elapsed_ms'] >= 0

    for result in body['results']:
        assert result['status'] == STATUS_COMPLETED
        assert result['reason'] is None
        analysis = result['analysis']
        assert analysis is not None
        assert analysis['symbol'] == result['symbol']
        assert analysis['signal'] in ('BUY', 'SELL', 'WAIT')


def test_every_result_carries_the_complete_single_analysis_payload(client):
    """The whole response is embedded, not a summary of it — that is what makes
    divergence between the scan and the analysis page structurally impossible."""
    body = _ok(client, symbols=['BTCUSDT'])
    analysis = body['results'][0]['analysis']
    for key in (
        'signal', 'quality', 'quality_grade', 'confidence', 'confidence_grade',
        'entry', 'sl', 'tp', 'risk_reward', 'tradeable', 'decision_reason',
        'decision_message', 'headline', 'reasons', 'analysis', 'intelligence',
        'quality_detail', 'confidence_detail', 'strategy_id', 'strategy_status',
        'generated_at',
    ):
        assert key in analysis, f'bulk dropped {key} from the single-analysis payload'
    assert analysis['analysis']['breakdown']


def test_strategy_status_is_production_and_comes_from_the_engine(client):
    """The status is resolved from the registry by the modules that actually
    ran. A client cannot relabel a run by asserting a status in its body."""
    body = _ok(client, symbols=['BTCUSDT'], strategy_status='experimental')
    assert body['strategy_status'] == StrategyStatus.PRODUCTION
    assert body['results'][0]['analysis']['strategy_status'] == StrategyStatus.PRODUCTION


def test_a_client_cannot_relabel_the_strategy_that_ran(client):
    """Naming the production strategy while claiming a different id elsewhere in
    the body changes nothing: only `strategy_id` is read, and only from the
    registry."""
    body = _ok(
        client, symbols=['BTCUSDT'],
        strategy_name='Totally Validated', strategy_version=99,
        preset='balanced',
    )
    analysis = body['results'][0]['analysis']
    assert body['strategy_id'] == PRODUCTION_ID
    assert analysis['strategy_id'] == PRODUCTION_ID
    assert analysis['strategy_name'] != 'Totally Validated'
    assert analysis['strategy_version'] != 99


# ─────────────────────────────────────────────
# 12. Nothing in the body can become a verdict
# ─────────────────────────────────────────────

INJECTED = {
    'signal': 'BUY',
    'tradeable': True,
    'quality': 100,
    'confidence': 100,
    'entry': 1.0,
    'sl': 0.5,
    'tp': [9.0, 99.0, 999.0],
    'risk_reward': 99.0,
    'decision_reason': 'tradeable',
    'strategy_status': 'production',
    'results': [{'symbol': 'BTCUSDT', 'status': 'completed'}],
    'completed': 99,
    'failed': 0,
    'requested': 99,
    'concurrency': 99,
}


def test_a_client_cannot_inject_a_verdict_through_the_request_body(client):
    """Every number in the response is the engine's. The extra fields below are
    ignored by the schema, and the control run proves the result is identical
    with and without them."""
    control = _ok(client, symbols=['BTCUSDT'])['results'][0]['analysis']
    polluted = _ok(client, symbols=['BTCUSDT'], **INJECTED)

    analysis = polluted['results'][0]['analysis']
    for field in ('signal', 'tradeable', 'quality', 'confidence', 'entry',
                  'sl', 'tp', 'risk_reward', 'decision_reason'):
        assert analysis[field] == control[field], (
            f'{field} changed when the client sent its own value'
        )

    # And the injected values specifically did not survive.
    assert polluted['requested'] == 1
    assert polluted['completed'] == len(polluted['results'])
    assert polluted['concurrency'] == resolved_concurrency()
    assert len(polluted['results']) == 1
    if analysis['signal'] == 'WAIT':
        assert analysis['tradeable'] is False
        assert analysis['entry'] is None
        assert analysis['sl'] is None
        assert analysis['tp'] == []


def test_an_untradeable_result_never_carries_levels(client):
    """The gate the UI depends on: tradeable=false means no entry to render."""
    body = _ok(client, symbols=list(LISTED_SYMBOLS[:4]))
    for result in body['results']:
        analysis = result['analysis']
        if not analysis['tradeable']:
            assert analysis['signal'] == 'WAIT'
            assert analysis['entry'] is None
            assert analysis['tp'] == []


# ─────────────────────────────────────────────
# 13. Isolation — one symbol never sinks the batch
# ─────────────────────────────────────────────

def test_one_failing_symbol_does_not_fail_the_batch(client):
    BulkStubProvider.failures = {
        'SOLUSDT': lambda: MarketDataUnavailableError('venue returned no candles'),
    }
    requested = ['BTCUSDT', 'SOLUSDT', 'ETHUSDT', 'XRPUSDT']
    body = _ok(client, symbols=requested)

    assert [r['symbol'] for r in body['results']] == requested
    assert body['requested'] == 4
    assert body['completed'] == 3
    assert body['failed'] == 1

    by_symbol = {r['symbol']: r for r in body['results']}
    assert by_symbol['SOLUSDT']['status'] == STATUS_FAILED
    assert by_symbol['SOLUSDT']['reason'] == REASON_DATA_UNAVAILABLE
    assert by_symbol['SOLUSDT']['analysis'] is None
    for symbol in ('BTCUSDT', 'ETHUSDT', 'XRPUSDT'):
        assert by_symbol[symbol]['status'] == STATUS_COMPLETED
        assert by_symbol[symbol]['analysis'] is not None


def test_a_batch_where_every_symbol_fails_is_still_a_200(client):
    """A failed symbol is a RESULT, not an error. The request succeeded: it ran
    the batch and is reporting what happened to each one."""
    BulkStubProvider.failures = {
        symbol: (lambda: MarketDataUnavailableError('down'))
        for symbol in ('BTCUSDT', 'ETHUSDT')
    }
    body = _ok(client, symbols=['BTCUSDT', 'ETHUSDT'])
    assert body['completed'] == 0
    assert body['failed'] == 2
    assert all(r['status'] == STATUS_FAILED for r in body['results'])


def test_an_unexpected_exception_is_contained_to_its_own_symbol(client):
    """Not a market condition — a bug. It still costs exactly one result."""
    BulkStubProvider.failures = {'ETHUSDT': lambda: RuntimeError('boom')}
    body = _ok(client, symbols=['BTCUSDT', 'ETHUSDT'])
    by_symbol = {r['symbol']: r for r in body['results']}
    assert by_symbol['ETHUSDT']['reason'] == REASON_FAILED
    assert by_symbol['BTCUSDT']['status'] == STATUS_COMPLETED


# ─────────────────────────────────────────────
# 14. The deadline — a hung venue call cannot pin the request open
# ─────────────────────────────────────────────

def test_a_hanging_provider_call_times_out_as_a_failed_symbol(client, monkeypatch):
    """A symbol whose venue call never answers is reported as failed and the
    request returns on its deadline.

    The production floor on the budget is 5s (asserted separately in
    `test_the_symbol_timeout_is_clamped`); it is lowered to 2s here so the test
    measures the deadline BEHAVIOUR without spending the production budget. The
    assertions are unchanged by that: the hung symbol must come back failed with
    a safe reason, the healthy symbols must still complete, and the response must
    arrive long before the hang itself ends.

    The hang is injected on a symbol that is NOT the benchmark. BTCUSDT is the
    reference every other analysis fetches for systemic context, so hanging it
    would stall the whole batch through a path that has nothing to do with the
    per-symbol deadline.
    """
    monkeypatch.setattr(bulk, '_TIMEOUT_MIN', 1)
    monkeypatch.setenv('BULK_SYMBOL_TIMEOUT_S', '2')
    assert resolved_symbol_timeout() == 2

    BulkStubProvider.hanging = {'SOLUSDT'}
    try:
        started = time.monotonic()
        body = _ok(client, symbols=['SOLUSDT', 'ETHUSDT', 'XRPUSDT'])
        elapsed = time.monotonic() - started
    finally:
        # Release the worker whatever happened, so it cannot outlive the suite.
        BulkStubProvider.release.set()

    by_symbol = {r['symbol']: r for r in body['results']}
    assert by_symbol['SOLUSDT']['status'] == STATUS_FAILED
    assert by_symbol['SOLUSDT']['reason'] == REASON_TIMEOUT
    assert by_symbol['SOLUSDT']['analysis'] is None
    # The rest of the batch is unaffected by the hang.
    assert by_symbol['ETHUSDT']['status'] == STATUS_COMPLETED
    assert by_symbol['XRPUSDT']['status'] == STATUS_COMPLETED
    assert body['completed'] == 2
    assert body['failed'] == 1
    # The request did not wait for the hang to end.
    assert elapsed < HANG_CEILING_SECONDS / 2, (
        f'bulk waited {elapsed:.1f}s on a hung call instead of its deadline'
    )


# ─────────────────────────────────────────────
# 15. A failure reason may never leak an internal
# ─────────────────────────────────────────────

def test_the_failure_reason_vocabulary_is_closed():
    """Unit-level: every exception the executor can see maps into the fixed set,
    and the mapping never returns the exception's own message."""
    for exc in (
        UnknownSymbolError(POISON),
        UnsupportedTimeframeError(POISON),
        MarketDataUnavailableError(POISON),
        ValueError(POISON),
        TimeoutError(POISON),
        RuntimeError(POISON),
        KeyError(POISON),
        ZeroDivisionError(POISON),
    ):
        reason = failure_reason(exc)
        assert reason in SAFE_REASONS, f'{type(exc).__name__} -> {reason!r}'
        for marker in LEAK_MARKERS:
            assert marker not in reason, f'{reason!r} leaks {marker!r}'


def test_no_failure_reason_leaks_an_internal_over_http(client):
    """Every failure mode in one request, each carrying a poisoned message."""
    BulkStubProvider.failures = {
        'BTCUSDT': lambda: UnknownSymbolError(POISON),
        'ETHUSDT': lambda: UnsupportedTimeframeError(POISON),
        'SOLUSDT': lambda: MarketDataUnavailableError(POISON),
        'XRPUSDT': lambda: ValueError(POISON),
        'ADAUSDT': lambda: RuntimeError(POISON),
    }
    symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT']
    body = _ok(client, symbols=symbols)
    assert body['failed'] == len(symbols)

    reasons = {r['symbol']: r['reason'] for r in body['results']}
    assert reasons == {
        'BTCUSDT': REASON_UNKNOWN_SYMBOL,
        'ETHUSDT': REASON_UNSUPPORTED_TIMEFRAME,
        'SOLUSDT': REASON_DATA_UNAVAILABLE,
        'XRPUSDT': REASON_NO_HISTORY,
        'ADAUSDT': REASON_FAILED,
    }
    # And nothing anywhere in the serialized response repeats the message.
    raw = _bulk(client, symbols=symbols).text
    for marker in ('SECRET', 'venue.example', 'Traceback', 'binance.py', 'line 118'):
        assert marker not in raw, f'the response body leaks {marker!r}'


def test_a_completed_result_never_carries_a_reason(client):
    """A reason is meaningful only on a failure; a completed result explains
    itself inside its own analysis payload."""
    body = _ok(client, symbols=['BTCUSDT'])
    assert body['results'][0]['reason'] is None


# ─────────────────────────────────────────────
# 16. Bounded concurrency
# ─────────────────────────────────────────────

@pytest.mark.parametrize('env_value,expected', [
    ('0', 1), ('-5', 1), ('1', 1), ('4', 4), ('8', 8),
    ('9', 8), ('40', 8), ('999999', 8),
    ('abc', 4), ('', 4), ('  ', 4), ('4.5', 4),
])
def test_resolved_concurrency_is_clamped_at_both_ends(monkeypatch, env_value, expected):
    """A typo of '40' for '4' must not be able to point forty simultaneous
    analyses at one exchange, and a zero must not stall the pool."""
    monkeypatch.setenv('BULK_MAX_CONCURRENCY', env_value)
    assert resolved_concurrency() == expected


def test_resolved_concurrency_defaults_when_unset(monkeypatch):
    monkeypatch.delenv('BULK_MAX_CONCURRENCY', raising=False)
    assert resolved_concurrency() == 4
    assert 1 <= MAX_CONCURRENT_ANALYSES <= 8


@pytest.mark.parametrize('env_value,expected', [
    ('0', 5), ('1', 5), ('5', 5), ('25', 25), ('120', 120),
    ('121', 120), ('99999', 120), ('nope', 25), ('', 25),
])
def test_the_symbol_timeout_is_clamped(monkeypatch, env_value, expected):
    monkeypatch.setenv('BULK_SYMBOL_TIMEOUT_S', env_value)
    assert resolved_symbol_timeout() == expected


def test_simultaneous_analyses_never_exceed_the_bound(client):
    """The real thing: instrument the provider and watch how many analyses are
    actually in flight at once.

    Every analysis occupies one worker thread and a thread is inside at most one
    fetch at a time, so the peak observed inside the provider IS the peak number
    of simultaneous analyses. The latency below exists only to make the overlap
    observable.
    """
    BulkStubProvider.fetch_delay = 0.06
    symbols = list(LISTED_SYMBOLS[:8])
    body = _ok(client, symbols=symbols)

    peak = BulkStubProvider.peak_in_flight
    bound = resolved_concurrency()

    assert body['completed'] == len(symbols)
    assert body['concurrency'] == bound
    assert peak <= bound, f'{peak} analyses ran at once against a bound of {bound}'
    assert peak <= MAX_CONCURRENT_ANALYSES
    # The pool itself never grew past the bound — timing-independent.
    assert len(BulkStubProvider.worker_threads) <= bound
    # ...and the test is not vacuous: work really did overlap.
    if bound > 1:
        assert peak >= 2, 'no overlap observed — the bound was never exercised'


def test_the_shipped_bounds_are_conservative():
    """Documented, deliberately small numbers: the ccxt client is shared across
    these threads, so the ceiling is what keeps that safe."""
    assert bulk._CONCURRENCY_DEFAULT == 4
    assert bulk._CONCURRENCY_MAX == 8
    assert MAX_BULK_SYMBOLS_PER_REQUEST == 10


# ─────────────────────────────────────────────
# 17-18. De-duplication and ordering
# ─────────────────────────────────────────────

def test_duplicate_symbols_are_de_duplicated(client):
    """A caller that sends the same pair twice pays for it once."""
    body = _ok(client, symbols=[
        'BTCUSDT', 'btcusdt', 'BTC/USDT', '  BTCUSDT  ', 'ETHUSDT', 'ETHUSDT',
    ])
    assert body['requested'] == 2
    assert [r['symbol'] for r in body['results']] == ['BTCUSDT', 'ETHUSDT']


def test_de_duplication_counts_against_the_ceiling_after_collapsing(client):
    """Eleven entries that collapse to two are two symbols, not eleven."""
    body = _ok(client, symbols=['BTCUSDT'] * 10 + ['ETHUSDT'])
    assert body['requested'] == 2


def test_results_preserve_the_requested_order(client):
    """The caller can zip the results against its own list without matching on
    symbol — including when a symbol in the middle fails."""
    requested = ['XRPUSDT', 'BTCUSDT', 'SOLUSDT', 'ETHUSDT', 'ADAUSDT']
    BulkStubProvider.failures = {
        'SOLUSDT': lambda: MarketDataUnavailableError('down'),
    }
    body = _ok(client, symbols=requested)
    assert [r['symbol'] for r in body['results']] == requested


def test_order_is_the_de_duplicated_request_order(client):
    """First occurrence wins, so the order a caller sent is the order it reads."""
    body = _ok(client, symbols=['ETHUSDT', 'BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
    assert [r['symbol'] for r in body['results']] == ['ETHUSDT', 'BTCUSDT', 'SOLUSDT']


# ─────────────────────────────────────────────
# 19. THE CENTRAL GUARANTEE
# ─────────────────────────────────────────────

#: The fields a user would notice disagreeing between the scan and the page.
VERDICT_FIELDS = (
    'signal', 'tradeable', 'quality', 'confidence', 'entry', 'sl', 'tp',
    'risk_reward', 'strategy_id', 'strategy_status', 'decision_reason',
)


@pytest.mark.parametrize('symbol', ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
def test_bulk_matches_the_single_analyze_route(client, symbol):
    """THE GUARANTEE THIS FEATURE RESTS ON.

    Bulk runs the production single-symbol path and returns its payload
    untouched. If these two ever disagreed, a user would read one verdict in the
    scan and a different one on the analysis page for the same setup.
    """
    single = _single(client, symbol)
    bulk_body = _ok(client, symbols=[symbol])
    scanned = bulk_body['results'][0]['analysis']

    assert {f: scanned[f] for f in VERDICT_FIELDS} == {
        f: single[f] for f in VERDICT_FIELDS
    }
    # The identity of the analysis matches too — same instrument, same ladder,
    # same module set.
    assert scanned['symbol'] == single['symbol']
    assert scanned['timeframe'] == single['timeframe']
    assert scanned['market'] == single['market']
    assert scanned['provider'] == single['provider']
    assert scanned['enabled_indicators'] == single['enabled_indicators']
    assert scanned['quality_grade'] == single['quality_grade']
    assert scanned['confidence_grade'] == single['confidence_grade']
    assert scanned['headline'] == single['headline']
    assert scanned['reasons'] == single['reasons']
    assert scanned['wait_reason'] == single['wait_reason']


def test_the_batch_agrees_with_the_single_route_for_every_symbol_at_once(client):
    """The same comparison across a whole batch, so an ordering or index bug in
    the executor cannot pass by lining one symbol up correctly."""
    symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT']
    expected = {s: _single(client, s) for s in symbols}
    body = _ok(client, symbols=symbols)

    for result in body['results']:
        single = expected[result['symbol']]
        scanned = result['analysis']
        assert {f: scanned[f] for f in VERDICT_FIELDS} == {
            f: single[f] for f in VERDICT_FIELDS
        }, f'{result["symbol"]} disagrees with its single analysis'


def test_bulk_never_marks_an_untradeable_setup_tradeable(client):
    """The decision layer is the only thing that may set this, on either route."""
    body = _ok(client, symbols=list(LISTED_SYMBOLS[:5]))
    for result in body['results']:
        analysis = result['analysis']
        if analysis['tradeable']:
            assert analysis['signal'] in ('BUY', 'SELL')
            assert analysis['entry'] is not None
            assert analysis['sl'] is not None
        else:
            assert analysis['signal'] == 'WAIT'


# ─────────────────────────────────────────────
# 20. Regression guard on the single route
# ─────────────────────────────────────────────

def test_the_single_analyze_route_still_works(client):
    """Bulk is additive. POST /api/analyze is untouched."""
    body = _single(client, 'BTCUSDT')
    assert body['signal'] in ('BUY', 'SELL', 'WAIT')
    assert body['symbol'] == 'BTCUSDT'
    assert body['timeframe'] == '15m'
    assert body['market'] == TEST_MARKET
    assert isinstance(body['quality'], int)
    assert isinstance(body['confidence'], int)
    assert body['strategy_id'] == PRODUCTION_ID
    assert body['strategy_status'] == StrategyStatus.PRODUCTION
    assert body['analysis']['breakdown']
    assert body['generated_at']


def test_the_single_route_is_unaffected_by_a_bulk_run(client):
    """Running a batch first must not disturb the single path — the pipeline is
    stateless and the provider is shared, so this is worth pinning."""
    before = _single(client, 'BTCUSDT')
    _ok(client, symbols=list(LISTED_SYMBOLS[:6]))
    after = _single(client, 'BTCUSDT')
    assert {f: before[f] for f in VERDICT_FIELDS} == {
        f: after[f] for f in VERDICT_FIELDS
    }


def test_the_single_route_still_accepts_an_experimental_strategy(client):
    """Bulk refuses experimental strategies. The single route must still serve
    them as research — the restriction is the batch route's, not a global one."""
    body = _single(client, 'BTCUSDT', strategy_id=EXPERIMENTAL_IDS[0])
    assert body['strategy_status'] == StrategyStatus.EXPERIMENTAL
    assert body['tradeable'] is False
    assert body['signal'] == 'WAIT'


def test_bulk_does_not_shadow_the_single_route(client):
    """Both routes are mounted; neither swallowed the other's path."""
    assert client.post('/api/analyze', json={
        'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m',
    }).status_code == 200
    assert _bulk(client, symbols=['BTCUSDT']).status_code == 200
