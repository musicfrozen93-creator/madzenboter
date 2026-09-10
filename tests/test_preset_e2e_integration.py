"""End-to-end: preset → enabled_indicators → engine → scoring → decision.

Section 9 of the audit asks for a REAL integration run — the FastAPI app, the
real pipeline, the real scorers and the real decision cascade — over a
controlled market fixture, with every stage of the chain observed in the
response rather than assumed.

Only the market data is stubbed. Nothing about the analysis is mocked.
"""

import pytest
from fastapi.testclient import TestClient

from analysis.modules import (
    ICT_CONFLUENCE,
    ICT_FVG,
    ICT_MODULES,
    MODULE_ORDER,
    MSNR_MODULES,
    PRESET_BALANCED,
    PRESET_CONSERVATIVE,
    PRESET_ICT_MSNR,
    REQUIRED_MODULES,
    preset_modules,
)
from analysis.scoring import (
    MIN_PROBABILITY_EDGE,
    MIN_TRADEABLE_CONFIDENCE,
    MIN_TRADEABLE_QUALITY,
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

TEST_MARKET = 'e2etest'


class E2EStubProvider(StubProvider):
    """A fixed, deterministic market fixture — the controlled snapshot."""

    name = 'e2estub'
    market = TEST_MARKET

    def __init__(self, settings=None):
        super().__init__(candles=make_uptrend(n=500))


@pytest.fixture
def client(settings):
    register(E2EStubProvider)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, headers={'X-Service-Key': TEST_SERVICE_KEY}) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    _REGISTRY.pop(E2EStubProvider.name, None)
    _DEFAULT_FOR_MARKET.pop(TEST_MARKET, None)
    reset_for_tests()


def _analyze(client, **overrides):
    payload = {'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m'}
    payload.update(overrides)
    res = client.post('/api/analyze', json=payload)
    assert res.status_code == 200, res.text
    return res.json()


# ─────────────────────────────────────────────
# The whole chain, observed in one response
# ─────────────────────────────────────────────

def test_every_stage_of_the_chain_is_present_and_consistent(client):
    """preset → enabled_indicators → engine → scoring → confluence → decision."""
    body = _analyze(client, preset=PRESET_ICT_MSNR)

    # 1. preset resolved, and echoed back
    assert body['preset'] == PRESET_ICT_MSNR
    enabled = set(body['enabled_indicators'])
    assert enabled == preset_modules(PRESET_ICT_MSNR)

    # 2. reached the engine: the scored breakdown covers exactly the enabled
    #    WEIGHTED modules — no more, no fewer.
    scored = {row['module'] for row in body['analysis']['breakdown']}
    assert scored == {m for m in MODULE_ORDER if m in enabled}

    # 3. scoring produced real 0-100 values
    for key in ('quality', 'confidence'):
        assert isinstance(body[key], int) and 0 <= body[key] <= 100
    assert body['quality_grade'] and body['confidence_grade']

    # 4. confluence panel is present for the enabled contextual layers
    assert body.get('ict_analysis') is not None
    assert body.get('msnr_analysis') is not None

    # 5. the final decision is authoritative and internally consistent
    assert body['decision_reason']
    assert isinstance(body['tradeable'], bool)
    if body['tradeable']:
        assert body['signal'] in ('BUY', 'SELL')
        assert body['quality'] >= MIN_TRADEABLE_QUALITY
        assert body['confidence'] >= MIN_TRADEABLE_CONFIDENCE
    else:
        assert body['signal'] == 'WAIT'


def test_the_decision_matches_the_scores_it_reports(client):
    """A signal below either floor must be WAIT, with the matching reason."""
    for preset in (PRESET_ICT_MSNR, PRESET_BALANCED, PRESET_CONSERVATIVE):
        body = _analyze(client, preset=preset)
        below_quality = body['quality'] < MIN_TRADEABLE_QUALITY
        below_confidence = body['confidence'] < MIN_TRADEABLE_CONFIDENCE
        if below_quality or below_confidence:
            assert body['tradeable'] is False, preset
            assert body['signal'] == 'WAIT', preset
            assert body['decision_reason'] != 'tradeable', preset


def test_switching_preset_changes_the_response_end_to_end(client):
    """Proof the preset is not cosmetic, observed through the HTTP contract."""
    ict = _analyze(client, preset=PRESET_ICT_MSNR)
    conservative = _analyze(client, preset=PRESET_CONSERVATIVE)

    assert set(ict['enabled_indicators']) != set(conservative['enabled_indicators'])
    ict_scored = {r['module'] for r in ict['analysis']['breakdown']}
    cons_scored = {r['module'] for r in conservative['analysis']['breakdown']}
    assert ict_scored != cons_scored
    # Conservative carries no ICT/MSNR context at all.
    assert conservative.get('ict_analysis') is None
    assert conservative.get('msnr_analysis') is None


def test_turning_one_contextual_toggle_off_reaches_the_engine(client):
    """A single unticked box must survive the whole chain, not just the UI."""
    full = _analyze(client, preset=PRESET_ICT_MSNR)
    without = _analyze(
        client,
        enabled_indicators=sorted(preset_modules(PRESET_ICT_MSNR) - {ICT_FVG}),
    )
    assert ICT_FVG in full['enabled_indicators']
    assert ICT_FVG not in without['enabled_indicators']
    # The weighted score is unchanged — the FVG carrier still scores it once.
    assert without['quality'] == full['quality']


def test_disabling_the_confluence_drops_it_from_the_response(client):
    full = _analyze(client, preset=PRESET_ICT_MSNR)
    without = _analyze(
        client,
        enabled_indicators=sorted(preset_modules(PRESET_ICT_MSNR) - {ICT_CONFLUENCE}),
    )
    assert full.get('ict_confluence') is not None
    assert without.get('ict_confluence') is None


def test_core_modules_are_scored_in_every_configuration(client):
    for payload in (
        {'preset': PRESET_ICT_MSNR},
        {'preset': PRESET_CONSERVATIVE},
        {'enabled_indicators': []},
        {},
    ):
        body = _analyze(client, **payload)
        scored = {r['module'] for r in body['analysis']['breakdown']}
        assert REQUIRED_MODULES <= scored, payload


# ─────────────────────────────────────────────
# The guarded constants, checked at the edge
# ─────────────────────────────────────────────

def test_the_audit_thresholds_are_unchanged():
    """Section 12: these must not move to manufacture more signals."""
    assert MIN_TRADEABLE_QUALITY == 60
    assert MIN_TRADEABLE_CONFIDENCE == 60
    assert MIN_PROBABILITY_EDGE == 10.0


def test_backward_compatibility_for_clients_that_send_no_config(client):
    """Omitting the config runs the production strategy, so legacy clients trade.

    A client written before strategies existed sends nothing. It must keep
    receiving normal BUY/SELL/WAIT verdicts, which means it must land on a
    PRODUCTION strategy — the old "every module" default would now be CUSTOM
    and permanently WAIT.
    """
    body = _analyze(client)
    assert set(body['enabled_indicators']) == preset_modules(PRESET_ICT_MSNR)
    assert body['strategy_status'] == 'production'
    # Every module remains reachable, just not by omission.
    assert preset_modules(PRESET_ICT_MSNR) != preset_modules(PRESET_BALANCED)
    explicit = _analyze(client, preset=PRESET_BALANCED)
    assert set(explicit['enabled_indicators']) == preset_modules(PRESET_BALANCED)


def test_the_registry_documents_the_dashboard_default(client):
    """Section 8: the default the dashboard uses is discoverable, not implicit."""
    body = client.get('/api/indicators').json()
    assert body['default_preset'] == PRESET_ICT_MSNR
    default = next(p for p in body['presets'] if p['id'] == body['default_preset'])
    assert default['default'] is True
    assert set(default['modules']) == preset_modules(PRESET_ICT_MSNR) - REQUIRED_MODULES


def test_contextual_toggles_never_enter_the_weighted_breakdown(client):
    """Section 4: no contextual key may appear as a scored module."""
    body = _analyze(client, preset=PRESET_ICT_MSNR)
    scored = {r['module'] for r in body['analysis']['breakdown']}
    assert not (scored & set(ICT_MODULES))
    assert not (scored & set(MSNR_MODULES))
