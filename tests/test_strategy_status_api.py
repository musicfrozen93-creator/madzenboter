"""The experimental restriction, enforced over the real HTTP contract.

Section 8: the backend is authoritative. A client calling the API directly —
naming an experimental strategy, hiding it behind a hand-built module list, or
mislabelling it as something else — must never obtain a tradeable BUY/SELL.
"""

import pytest
from fastapi.testclient import TestClient

from analysis.decision import CODE_CUSTOM_STRATEGY, CODE_EXPERIMENTAL_STRATEGY
from analysis.strategies import (
    CUSTOM_STRATEGY_ID,
    STRATEGY_ORDER,
    StrategyStatus,
    strategy_modules,
)
from api.app import create_app
from api.dependencies import get_settings
from providers.registry import (
    _DEFAULT_FOR_MARKET,
    _REGISTRY,
    register,
    reset_for_tests,
)
from tests.conftest import TEST_SERVICE_KEY
from tests.fakes import StubProvider, make_downtrend, make_uptrend

TEST_MARKET = 'statustest'

EXPERIMENTAL_IDS = [
    s.strategy_id for s in STRATEGY_ORDER if s.status == StrategyStatus.EXPERIMENTAL
]


class StatusStubProvider(StubProvider):
    name = 'statusstub'
    market = TEST_MARKET

    def __init__(self, settings=None):
        super().__init__(candles=make_uptrend(n=500))


@pytest.fixture
def client(settings):
    register(StatusStubProvider)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, headers={'X-Service-Key': TEST_SERVICE_KEY}) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    _REGISTRY.pop(StatusStubProvider.name, None)
    _DEFAULT_FOR_MARKET.pop(TEST_MARKET, None)
    reset_for_tests()


def _analyze(client, **overrides):
    payload = {'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m'}
    payload.update(overrides)
    res = client.post('/api/analyze', json=payload)
    assert res.status_code == 200, res.text
    return res.json()


# ─────────────────────────────────────────────
# 16. The catalogue exposes status
# ─────────────────────────────────────────────

def test_the_catalogue_publishes_every_status(client):
    body = client.get('/api/indicators').json()
    by_id = {s['strategy_id']: s for s in body['strategies']}
    assert by_id['ICT_MSNR_V1']['status'] == 'production'
    assert by_id['ICT_MSNR_V1']['recommended'] is True
    for strategy_id in EXPERIMENTAL_IDS:
        assert by_id[strategy_id]['status'] == 'experimental'
        assert by_id[strategy_id]['recommended'] is False
    assert by_id[CUSTOM_STRATEGY_ID]['status'] == 'custom'


def test_the_default_strategy_is_a_production_one(client):
    body = client.get('/api/indicators').json()
    default = next(
        s for s in body['strategies'] if s['strategy_id'] == body['default_strategy']
    )
    assert default['status'] == 'production'


# ─────────────────────────────────────────────
# 5–9, 18. Experimental over HTTP
# ─────────────────────────────────────────────

@pytest.mark.parametrize('strategy_id', EXPERIMENTAL_IDS)
def test_an_experimental_strategy_still_returns_a_full_diagnostic(client, strategy_id):
    body = _analyze(client, strategy_id=strategy_id)
    assert body['strategy_status'] == 'experimental'
    # The research payload is intact: scores, breakdown and bias all present.
    assert isinstance(body['quality'], int) and isinstance(body['confidence'], int)
    assert body['analysis']['breakdown']
    assert set(body['enabled_indicators']) == strategy_modules(strategy_id)


@pytest.mark.parametrize('strategy_id', EXPERIMENTAL_IDS)
def test_an_experimental_strategy_is_never_tradeable_over_http(client, strategy_id):
    body = _analyze(client, strategy_id=strategy_id)
    assert body['tradeable'] is False
    assert body['signal'] == 'WAIT'
    assert body['decision_reason'] == CODE_EXPERIMENTAL_STRATEGY


@pytest.mark.parametrize('strategy_id', EXPERIMENTAL_IDS)
@pytest.mark.parametrize('candles_name', ['uptrend', 'downtrend'])
def test_no_market_condition_makes_an_experimental_strategy_trade(
    client, settings, strategy_id, candles_name
):
    """Neither a clean uptrend nor a clean downtrend produces BUY or SELL."""
    provider = StatusStubProvider()
    if candles_name == 'downtrend':
        provider.candles = make_downtrend(n=500)
    body = _analyze(client, strategy_id=strategy_id)
    assert body['signal'] not in ('BUY', 'SELL')
    assert body['tradeable'] is False


def test_a_direct_api_call_cannot_bypass_the_restriction(client):
    """Section 18: the frontend is not what enforces this."""
    # 1. Name the strategy — blocked.
    named = _analyze(client, strategy_id='MOMENTUM_V1')
    assert named['tradeable'] is False

    # 2. Smuggle its modules without naming it — still blocked, because the
    #    status is derived from what the engine actually ran.
    smuggled = _analyze(
        client, enabled_indicators=sorted(strategy_modules('MOMENTUM_V1')),
    )
    assert smuggled['strategy_id'] == 'MOMENTUM_V1'
    assert smuggled['strategy_status'] == 'experimental'
    assert smuggled['tradeable'] is False
    assert smuggled['decision_reason'] == CODE_EXPERIMENTAL_STRATEGY

    # 2b. Declaring CUSTOM over the same modules is MORE restrictive, not less.
    declared = _analyze(
        client, strategy_id=CUSTOM_STRATEGY_ID,
        enabled_indicators=sorted(strategy_modules('MOMENTUM_V1')),
    )
    assert declared['strategy_status'] == 'custom'
    assert declared['tradeable'] is False

    # 3. Claim it is the production strategy while sending Momentum's modules —
    #    the registry resolves the NAME, so this just runs ICT + MSNR.
    mislabelled = _analyze(
        client, strategy_id='ICT_MSNR_V1',
        enabled_indicators=sorted(strategy_modules('MOMENTUM_V1')),
    )
    assert set(mislabelled['enabled_indicators']) == strategy_modules('ICT_MSNR_V1')
    assert mislabelled['strategy_status'] == 'production'


def test_a_client_cannot_assert_its_own_status(client):
    """Status is never read from the request."""
    body = _analyze(
        client, strategy_id='MOMENTUM_V1',
        strategy_status='production', status='production',
    )
    assert body['strategy_status'] == 'experimental'
    assert body['tradeable'] is False


# ─────────────────────────────────────────────
# 11–12. Production and Custom over HTTP
# ─────────────────────────────────────────────

def test_the_production_strategy_follows_the_normal_cascade(client):
    body = _analyze(client, strategy_id='ICT_MSNR_V1')
    assert body['strategy_status'] == 'production'
    assert body['decision_reason'] != CODE_EXPERIMENTAL_STRATEGY
    if body['tradeable']:
        assert body['signal'] in ('BUY', 'SELL')
    else:
        assert body['signal'] == 'WAIT'


def test_custom_runs_fully_but_is_never_tradeable(client):
    body = _analyze(
        client, strategy_id=CUSTOM_STRATEGY_ID,
        enabled_indicators=['rsi', 'macd', 'volume'],
    )
    assert body['strategy_status'] == 'custom'
    # The full research payload is still returned.
    assert isinstance(body['quality'], int) and isinstance(body['confidence'], int)
    assert body['analysis']['breakdown']
    # But the verdict is always WAIT, with its own reason code.
    assert body['signal'] == 'WAIT'
    assert body['tradeable'] is False
    assert body['decision_reason'] == CODE_CUSTOM_STRATEGY


def test_no_custom_module_list_can_produce_a_tradeable_signal(client):
    for modules in (
        ['rsi'], ['macd', 'volume'], ['elliott', 'fibonacci', 'pattern', 'adx'],
    ):
        body = _analyze(
            client, strategy_id=CUSTOM_STRATEGY_ID, enabled_indicators=modules,
        )
        assert body['tradeable'] is False, modules
        assert body['signal'] == 'WAIT', modules


def test_custom_cannot_be_disguised_as_production(client):
    """Naming the production strategy runs IT, not the caller's module list."""
    body = _analyze(
        client, strategy_id='ICT_MSNR_V1', enabled_indicators=['rsi'],
    )
    assert set(body['enabled_indicators']) == strategy_modules('ICT_MSNR_V1')
    assert body['strategy_status'] == 'production'
    # And a bogus status field in the request changes nothing.
    smuggled = _analyze(
        client, strategy_id=CUSTOM_STRATEGY_ID, enabled_indicators=['rsi'],
        strategy_status='production', status='production', tradeable=True,
    )
    assert smuggled['strategy_status'] == 'custom'
    assert smuggled['tradeable'] is False


def test_ict_msnr_is_the_only_strategy_that_can_trade_over_http(client):
    """The production matrix, asserted end to end."""
    tradeable_statuses = set()
    for strategy in STRATEGY_ORDER:
        body = _analyze(client, strategy_id=strategy.strategy_id)
        if body['tradeable']:
            tradeable_statuses.add(body['strategy_id'])
    custom = _analyze(
        client, strategy_id=CUSTOM_STRATEGY_ID, enabled_indicators=['rsi', 'volume'],
    )
    assert not custom['tradeable']
    assert tradeable_statuses <= {'ICT_MSNR_V1'}


def test_clients_sending_nothing_get_the_production_strategy(client):
    """An older client with no strategy_id runs ICT + MSNR and can still trade.

    Omission is the ONE case that resolves to the production default. Any
    explicit configuration — including an explicit module list — stays exactly
    what the caller asked for, and stays non-tradeable when it is not a
    validated strategy.
    """
    body = _analyze(client)
    assert body['strategy_id'] == 'ICT_MSNR_V1'
    assert body['strategy_status'] == 'production'
    assert body['analysis']['breakdown']
    assert isinstance(body['quality'], int)
    assert body['decision_reason'] not in (
        CODE_CUSTOM_STRATEGY, CODE_EXPERIMENTAL_STRATEGY
    )


# ─────────────────────────────────────────────
# 12, 14. Research metadata travels with the result
# ─────────────────────────────────────────────

@pytest.mark.parametrize('strategy_id', ['ICT_MSNR_V1'] + EXPERIMENTAL_IDS)
def test_a_result_carries_everything_a_backtest_would_need(client, strategy_id):
    """Section 14: enough metadata to test these strategies independently later.

    No performance figures are invented — only the identifying fields a future
    backtest needs to attribute a result to a strategy definition.
    """
    body = _analyze(client, strategy_id=strategy_id)
    for field in (
        'strategy_id', 'strategy_name', 'strategy_version', 'strategy_status',
        'market', 'symbol', 'timeframe', 'signal', 'direction_bias',
        'quality', 'confidence', 'enabled_modules', 'generated_at',
    ):
        assert field in body, f'{strategy_id} missing {field}'
    # Trade levels are present whenever the engine produced them.
    for field in ('entry', 'sl', 'tp', 'risk_reward'):
        assert field in body


def test_the_saved_snapshot_can_state_the_strategy_and_its_status(client):
    body = _analyze(client, strategy_id='MOMENTUM_V1')
    assert body['strategy_id'] == 'MOMENTUM_V1'
    assert body['strategy_version'] == 1
    assert body['strategy_status'] == 'experimental'
