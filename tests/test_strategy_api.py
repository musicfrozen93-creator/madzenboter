"""Strategy selection over the real HTTP contract.

Section 3 of the brief: a strategy button must not be cosmetic. These tests
drive the actual FastAPI app and prove the chain end to end —

    strategy_id → registry resolution → enabled modules → engine → scoring
                → confluence → final decision → strategy metadata in the response

and that the BACKEND is authoritative: a client names a strategy, it never
describes one.
"""

import pytest
from fastapi.testclient import TestClient

from analysis.modules import ICT_MODULES, MODULE_ORDER, MSNR_MODULES, REQUIRED_MODULES
from analysis.strategies import (
    CUSTOM_STRATEGY_ID,
    DEFAULT_STRATEGY_ID,
    STRATEGY_ORDER,
    describe_strategies,
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
from tests.fakes import StubProvider, make_uptrend

TEST_MARKET = 'strategytest'


class StrategyStubProvider(StubProvider):
    name = 'strategystub'
    market = TEST_MARKET

    def __init__(self, settings=None):
        super().__init__(candles=make_uptrend(n=500))


@pytest.fixture
def client(settings):
    register(StrategyStubProvider)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, headers={'X-Service-Key': TEST_SERVICE_KEY}) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    _REGISTRY.pop(StrategyStubProvider.name, None)
    _DEFAULT_FOR_MARKET.pop(TEST_MARKET, None)
    reset_for_tests()


def _analyze(client, **overrides):
    payload = {'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m'}
    payload.update(overrides)
    res = client.post('/api/analyze', json=payload)
    assert res.status_code == 200, res.text
    return res.json()


# ─────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────

def test_the_catalogue_is_published_for_the_dashboard(client):
    body = client.get('/api/indicators').json()
    ids = [s['strategy_id'] for s in body['strategies']]
    assert ids == [s.strategy_id for s in STRATEGY_ORDER] + [CUSTOM_STRATEGY_ID]
    assert body['default_strategy'] == DEFAULT_STRATEGY_ID


def test_published_definitions_match_the_engine_exactly(client):
    """The frontend cannot disagree with the backend about a strategy."""
    body = client.get('/api/indicators').json()
    for entry in body['strategies']:
        if entry['strategy_id'] == CUSTOM_STRATEGY_ID:
            continue
        expected = strategy_modules(entry['strategy_id']) - REQUIRED_MODULES
        assert set(entry['enabled_modules']) == expected, entry['strategy_id']


def test_a_client_can_rebuild_any_strategy_from_discovery_alone(client):
    body = client.get('/api/indicators').json()
    for entry in body['strategies']:
        if entry['strategy_id'] == CUSTOM_STRATEGY_ID:
            continue
        rebuilt = set(entry['enabled_modules']) | set(entry['locked_modules'])
        assert rebuilt == strategy_modules(entry['strategy_id'])


# ─────────────────────────────────────────────
# The id reaches the engine and is respected
# ─────────────────────────────────────────────

@pytest.mark.parametrize('strategy', STRATEGY_ORDER, ids=lambda s: s.strategy_id)
def test_each_strategy_id_resolves_and_runs(client, strategy):
    body = _analyze(client, strategy_id=strategy.strategy_id)
    assert body['strategy_id'] == strategy.strategy_id
    assert body['strategy_name'] == strategy.name
    assert body['strategy_version'] == strategy.version
    assert set(body['enabled_indicators']) == strategy.modules
    assert set(body['enabled_modules']) == strategy.modules
    # The engine scored exactly the enabled WEIGHTED modules.
    scored = {r['module'] for r in body['analysis']['breakdown']}
    assert scored == {m for m in MODULE_ORDER if m in strategy.modules}


def test_the_registry_overrides_a_module_list_sent_alongside_an_id(client):
    """A client names a strategy; it does not get to define one."""
    body = _analyze(
        client,
        strategy_id='MOMENTUM_V1',
        enabled_indicators=['elliott', 'fibonacci', 'pattern'],
    )
    assert body['strategy_id'] == 'MOMENTUM_V1'
    assert set(body['enabled_indicators']) == strategy_modules('MOMENTUM_V1')
    assert 'elliott' not in body['enabled_indicators']


def test_an_unknown_strategy_id_is_rejected_not_substituted(client):
    """Fails closed. Substituting the default would let a request for a
    NON-tradeable strategy — a typo, a renamed id, a stale client sending
    MOMENTUM_V2 — come back as a tradeable production signal."""
    for bogus in ('TOTALLY_MADE_UP_V9', 'MOMENTUM_V2', 'PWN', 'ICT_MSNR_V9'):
        res = client.post('/api/analyze', json={
            'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m',
            'strategy_id': bogus,
        })
        assert res.status_code == 400, bogus
        assert 'strategy_id' in res.json()['detail']


def test_selecting_different_strategies_changes_what_is_analysed(client):
    seen = {}
    for strategy in STRATEGY_ORDER:
        body = _analyze(client, strategy_id=strategy.strategy_id)
        seen[strategy.strategy_id] = frozenset(body['enabled_indicators'])
    assert len(set(seen.values())) == len(seen), 'two strategies analysed the same set'


def test_only_one_strategy_runs_per_analysis(client):
    """Ids never combine into a union — one primary strategy per analysis."""
    ict = frozenset(_analyze(client, strategy_id='ICT_MSNR_V1')['enabled_indicators'])
    momentum = frozenset(_analyze(client, strategy_id='MOMENTUM_V1')['enabled_indicators'])
    assert ict != momentum
    # A comma-joined pair is not a strategy id, and is rejected rather than
    # resolved to either one or to their union.
    res = client.post('/api/analyze', json={
        'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m',
        'strategy_id': 'ICT_MSNR_V1,MOMENTUM_V1',
    })
    assert res.status_code == 400


# ─────────────────────────────────────────────
# Custom mode
# ─────────────────────────────────────────────

def test_custom_mode_uses_the_explicit_module_list(client):
    body = _analyze(
        client, strategy_id=CUSTOM_STRATEGY_ID,
        enabled_indicators=['rsi', 'macd', 'volume'],
    )
    assert set(body['enabled_indicators']) == REQUIRED_MODULES | {'rsi', 'macd', 'volume'}
    assert body['strategy_id'] == CUSTOM_STRATEGY_ID
    assert body['strategy_name'] == 'Custom'


def test_custom_mode_still_forces_the_core_modules_on(client):
    body = _analyze(client, strategy_id=CUSTOM_STRATEGY_ID, enabled_indicators=[])
    assert set(body['enabled_indicators']) == REQUIRED_MODULES


def test_a_declared_custom_selection_stays_custom(client):
    """Declaring CUSTOM is honoured even when the modules match a strategy.

    Otherwise one UI click — selecting Custom without touching a checkbox, so
    the selection still equals the production strategy — would silently produce
    a tradeable production signal while the UI reads "Custom / research only".
    """
    body = _analyze(
        client, strategy_id=CUSTOM_STRATEGY_ID,
        enabled_indicators=sorted(strategy_modules('ICT_MSNR_V1')),
    )
    assert body['strategy_id'] == CUSTOM_STRATEGY_ID
    assert body['strategy_status'] == 'custom'
    assert body['tradeable'] is False


def test_an_undeclared_selection_is_named_by_its_modules(client):
    """Without a declaration the module set decides — it IS that methodology."""
    body = _analyze(client, enabled_indicators=sorted(strategy_modules('MOMENTUM_V1')))
    assert body['strategy_id'] == 'MOMENTUM_V1'
    assert body['strategy_status'] == 'experimental'
    assert body['tradeable'] is False


def test_hostile_module_names_cannot_reach_the_engine(client):
    body = _analyze(
        client, strategy_id=CUSTOM_STRATEGY_ID,
        enabled_indicators=['__import__', 'DROP TABLE', ''],
    )
    assert set(body['enabled_indicators']) == REQUIRED_MODULES


# ─────────────────────────────────────────────
# Backward compatibility
# ─────────────────────────────────────────────

def test_clients_that_send_no_strategy_still_get_tradeable_signals(client):
    """The Telegram bot and any external client keep working.

    They configure nothing, so they run the production strategy and stay on the
    normal decision cascade rather than being blocked as an unvalidated
    configuration.
    """
    body = _analyze(client)
    assert body['strategy_id'] == DEFAULT_STRATEGY_ID
    assert body['strategy_status'] == 'production'
    assert set(body['enabled_indicators']) == strategy_modules(DEFAULT_STRATEGY_ID)


def test_the_legacy_preset_field_still_works(client):
    body = _analyze(client, preset='ict_msnr')
    assert set(body['enabled_indicators']) == strategy_modules('ICT_MSNR_V1')
    assert body['strategy_id'] == 'ICT_MSNR_V1'


def test_a_strategy_id_takes_precedence_over_a_legacy_preset(client):
    body = _analyze(client, strategy_id='MOMENTUM_V1', preset='ict_msnr')
    assert body['strategy_id'] == 'MOMENTUM_V1'


def test_every_response_carries_the_strategy_metadata(client):
    for payload in (
        {'strategy_id': 'ICT_MSNR_V1'},
        {'strategy_id': CUSTOM_STRATEGY_ID, 'enabled_indicators': ['rsi']},
        {'preset': 'balanced'},
        {},
    ):
        body = _analyze(client, **payload)
        assert body['strategy_id'] and body['strategy_name']
        assert isinstance(body['strategy_version'], int)
        assert body['enabled_modules'] == body['enabled_indicators']


def test_the_decision_gates_still_govern_the_verdict(client):
    """Strategy selection changes WHAT is analysed, never the gates."""
    from analysis.scoring import MIN_TRADEABLE_CONFIDENCE, MIN_TRADEABLE_QUALITY
    for strategy in STRATEGY_ORDER:
        body = _analyze(client, strategy_id=strategy.strategy_id)
        if body['tradeable']:
            assert body['quality'] >= MIN_TRADEABLE_QUALITY
            assert body['confidence'] >= MIN_TRADEABLE_CONFIDENCE
            assert body['signal'] in ('BUY', 'SELL')
        else:
            assert body['signal'] == 'WAIT'


def test_contextual_modules_never_appear_as_scored_votes(client):
    for strategy in STRATEGY_ORDER:
        body = _analyze(client, strategy_id=strategy.strategy_id)
        scored = {r['module'] for r in body['analysis']['breakdown']}
        assert not (scored & set(ICT_MODULES)), strategy.strategy_id
        assert not (scored & set(MSNR_MODULES)), strategy.strategy_id
