"""End-to-end contract for the strategy preset system.

Section 3 of the audit: a preset must NOT be cosmetic. These tests drive the
real FastAPI app and prove that the configuration a client selects actually
reaches the engine and changes what it analyses:

    preset selection → API request → config parsing → module registry
                     → scoring → the enabled_indicators the response reports

They also pin the discovery contract (`GET /api/indicators` publishes the
presets and names the default) that the dashboard reads instead of defining
its own idea of what "ICT + MSNR" means.
"""

import pytest
from fastapi.testclient import TestClient

from analysis.modules import (
    DEFAULT_PRESET,
    ELLIOTT,
    ICT_MODULES,
    MODULE_ORDER,
    MSNR_MODULES,
    PRESET_BALANCED,
    PRESET_CONSERVATIVE,
    PRESET_CUSTOM,
    PRESET_ICT_MSNR,
    REQUIRED_MODULES,
    RSI,
    preset_modules,
)
from analysis.strategies import DEFAULT_STRATEGY_ID, strategy_modules
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

TEST_MARKET = 'presettest'


class PresetStubProvider(StubProvider):
    """A registered stub so /api/analyze runs without any network."""

    name = 'presetstub'
    market = TEST_MARKET

    def __init__(self, settings=None):
        super().__init__(candles=make_uptrend(n=500))


@pytest.fixture
def client(settings):
    register(PresetStubProvider)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, headers={'X-Service-Key': TEST_SERVICE_KEY}) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    _REGISTRY.pop(PresetStubProvider.name, None)
    _DEFAULT_FOR_MARKET.pop(TEST_MARKET, None)
    reset_for_tests()


def _analyze(client, **overrides):
    payload = {'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m'}
    payload.update(overrides)
    res = client.post('/api/analyze', json=payload)
    assert res.status_code == 200, res.text
    return res.json()


# ─────────────────────────────────────────────
# Discovery: the registry publishes the presets
# ─────────────────────────────────────────────

def test_indicators_endpoint_publishes_presets(client):
    body = client.get('/api/indicators').json()
    ids = [p['id'] for p in body['presets']]
    assert set(ids) == {PRESET_ICT_MSNR, PRESET_BALANCED, PRESET_CONSERVATIVE}
    assert body['default_preset'] == DEFAULT_PRESET == PRESET_ICT_MSNR


def test_published_preset_modules_match_the_engine(client):
    body = client.get('/api/indicators').json()
    for preset in body['presets']:
        expected = preset_modules(preset['id']) - REQUIRED_MODULES
        assert set(preset['modules']) == expected, preset['id']


def test_the_dashboard_can_build_the_default_from_discovery_alone(client):
    """A client with no hardcoded knowledge can reproduce the default exactly."""
    body = client.get('/api/indicators').json()
    default = next(p for p in body['presets'] if p['id'] == body['default_preset'])
    required = [i['key'] for i in body['indicators'] if i['required']]
    reconstructed = set(default['modules']) | set(required)
    assert reconstructed == preset_modules(DEFAULT_PRESET)


# ─────────────────────────────────────────────
# The selection reaches the engine
# ─────────────────────────────────────────────

def test_an_explicit_module_list_is_respected(client):
    chosen = sorted(preset_modules(PRESET_ICT_MSNR))
    body = _analyze(client, enabled_indicators=chosen)
    assert set(body['enabled_indicators']) == set(chosen)
    assert body['preset'] == PRESET_ICT_MSNR


def test_a_preset_id_resolves_to_that_preset(client):
    body = _analyze(client, preset=PRESET_ICT_MSNR)
    assert set(body['enabled_indicators']) == preset_modules(PRESET_ICT_MSNR)
    assert body['preset'] == PRESET_ICT_MSNR


def test_an_explicit_list_wins_over_a_preset_id(client):
    body = _analyze(client, preset=PRESET_BALANCED, enabled_indicators=[RSI])
    assert set(body['enabled_indicators']) == REQUIRED_MODULES | {RSI}
    assert body['preset'] == PRESET_CUSTOM


def test_an_unknown_preset_id_is_rejected(client):
    """Fails closed: substituting the production preset would hand a tradeable
    signal to a caller who asked for something the registry does not define."""
    res = client.post('/api/analyze', json={
        'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m',
        'preset': 'wishful-thinking',
    })
    assert res.status_code == 400
    assert 'preset' in res.json()['detail'].lower()


def test_omitting_the_config_runs_the_production_strategy(client):
    """An unconfigured request runs ICT + MSNR, the production default.

    It used to mean "every module" — but that set matches no registered
    strategy, so under the production-safety policy it would resolve to CUSTOM
    and never produce a tradeable signal. A client that configured nothing gets
    the strategy this product stands behind instead.
    """
    body = _analyze(client)
    assert set(body['enabled_indicators']) == strategy_modules(DEFAULT_STRATEGY_ID)
    assert body['strategy_id'] == DEFAULT_STRATEGY_ID
    assert body['strategy_status'] == 'production'


def test_every_module_is_still_reachable_by_asking_for_it(client):
    """The historical module set remains available under the balanced preset."""
    body = _analyze(client, preset=PRESET_BALANCED)
    assert set(body['enabled_indicators']) == preset_modules(PRESET_BALANCED)
    assert body['preset'] == PRESET_BALANCED


# ─────────────────────────────────────────────
# The engine actually respects it
# ─────────────────────────────────────────────

def test_a_disabled_module_is_absent_from_the_scored_breakdown(client):
    body = _analyze(client, preset=PRESET_ICT_MSNR)
    scored = {row['module'] for row in body['analysis']['breakdown']}
    for off in (ELLIOTT, RSI, 'macd', 'adx', 'pattern', 'fibonacci'):
        assert off not in scored, f'{off} should not be scored under ICT + MSNR'
    assert {'trend', 'structure'} <= scored


def test_switching_preset_changes_the_scored_module_set(client):
    ict = {r['module'] for r in _analyze(client, preset=PRESET_ICT_MSNR)['analysis']['breakdown']}
    conservative = {
        r['module'] for r in
        _analyze(client, preset=PRESET_CONSERVATIVE)['analysis']['breakdown']
    }
    assert ict != conservative
    assert RSI in conservative and RSI not in ict


def test_ict_and_msnr_panels_appear_under_the_default_preset(client):
    body = _analyze(client, preset=PRESET_ICT_MSNR)
    assert body.get('ict_analysis') is not None
    assert body.get('msnr_analysis') is not None


def test_ict_and_msnr_panels_are_absent_under_conservative(client):
    """A disabled category never shows up as active evidence."""
    body = _analyze(client, preset=PRESET_CONSERVATIVE)
    assert not (set(body['enabled_indicators']) & set(ICT_MODULES))
    assert not (set(body['enabled_indicators']) & set(MSNR_MODULES))
    assert body.get('ict_analysis') is None
    assert body.get('msnr_analysis') is None


def test_core_modules_survive_a_hostile_request(client):
    body = _analyze(client, enabled_indicators=['__import__', 'DROP TABLE', ''])
    assert set(body['enabled_indicators']) == REQUIRED_MODULES


def test_every_reported_module_is_a_real_engine_module(client):
    body = _analyze(client, preset=PRESET_ICT_MSNR)
    known = set(MODULE_ORDER) | set(ICT_MODULES) | set(MSNR_MODULES)
    assert set(body['enabled_indicators']) <= known


# ─────────────────────────────────────────────
# The decision contract is unchanged
# ─────────────────────────────────────────────

def test_the_response_still_carries_the_authoritative_decision_fields(client):
    body = _analyze(client, preset=PRESET_ICT_MSNR)
    assert isinstance(body['tradeable'], bool)
    assert body['decision_reason']
    if body['signal'] == 'WAIT':
        assert body['tradeable'] is False
    if body['tradeable']:
        assert body['signal'] in ('BUY', 'SELL')


def test_a_wait_never_publishes_a_tradeable_flag(client):
    for preset in (PRESET_ICT_MSNR, PRESET_BALANCED, PRESET_CONSERVATIVE):
        body = _analyze(client, preset=preset)
        if body['signal'] == 'WAIT':
            assert body['tradeable'] is False
