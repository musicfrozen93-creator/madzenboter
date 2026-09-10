"""An unconfigured request runs the production strategy.

A client written before strategies existed sends no strategy_id, no
enabled_indicators and no preset. That used to mean "every module" — a set
matching no registered strategy, which under the production-safety policy
resolves to CUSTOM and never trades. Such a client (the Telegram bot, any
external consumer) would have silently stopped receiving signals.

An unconfigured request therefore resolves to ICT + MSNR, the production
strategy. OMISSION is the only thing that changed: every explicit
configuration — a named strategy, a module list, a legacy preset — resolves
exactly as it did before.
"""

import pytest
from fastapi.testclient import TestClient

from analysis.decision import CODE_CUSTOM_STRATEGY, CODE_EXPERIMENTAL_STRATEGY
from analysis.modules import ALL_MODULE_KEYS, REQUIRED_MODULES
from analysis.scoring import MIN_TRADEABLE_CONFIDENCE, MIN_TRADEABLE_QUALITY
from analysis.strategies import (
    CUSTOM_STRATEGY_ID,
    DEFAULT_STRATEGY_ID,
    STRATEGIES,
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
from tests.fakes import StubProvider, make_uptrend

TEST_MARKET = 'noconfigtest'

EXPERIMENTAL_IDS = [
    s.strategy_id for s in STRATEGY_ORDER if s.status == StrategyStatus.EXPERIMENTAL
]


class NoConfigStubProvider(StubProvider):
    name = 'noconfigstub'
    market = TEST_MARKET

    def __init__(self, settings=None):
        super().__init__(candles=make_uptrend(n=500))


@pytest.fixture
def client(settings):
    register(NoConfigStubProvider)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, headers={'X-Service-Key': TEST_SERVICE_KEY}) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    _REGISTRY.pop(NoConfigStubProvider.name, None)
    _DEFAULT_FOR_MARKET.pop(TEST_MARKET, None)
    reset_for_tests()


def _post(client, payload):
    res = client.post('/api/analyze', json=payload)
    assert res.status_code == 200, res.text
    return res.json()


def _minimal():
    """The smallest legal request — no configuration of any kind."""
    return {'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m'}


# ─────────────────────────────────────────────
# 1–3, 10. The unconfigured request
# ─────────────────────────────────────────────

def test_an_empty_request_resolves_to_the_production_strategy(client):
    body = _post(client, _minimal())
    assert body['strategy_id'] == 'ICT_MSNR_V1'
    assert body['strategy_name'] == 'ICT + MSNR'
    assert body['strategy_version'] == STRATEGIES['ICT_MSNR_V1'].version
    assert body['strategy_status'] == 'production'


def test_no_strategy_id_and_no_modules_resolves_to_the_default(client):
    body = _post(client, {**_minimal(), 'provider': None})
    assert body['strategy_id'] == DEFAULT_STRATEGY_ID
    assert set(body['enabled_indicators']) == strategy_modules(DEFAULT_STRATEGY_ID)


def test_explicit_nulls_are_still_no_configuration(client):
    """A client sending the keys with null values has configured nothing."""
    body = _post(client, {
        **_minimal(), 'strategy_id': None, 'enabled_indicators': None, 'preset': None,
    })
    assert body['strategy_id'] == DEFAULT_STRATEGY_ID
    assert body['strategy_status'] == 'production'


def test_the_response_names_the_strategy_for_the_no_config_case(client):
    """Requirement 10: the API states which strategy ran, never implicitly."""
    body = _post(client, _minimal())
    for field in ('strategy_id', 'strategy_name', 'strategy_version', 'strategy_status'):
        assert body[field], field
    assert body['enabled_modules'] == body['enabled_indicators']


def test_the_no_config_default_is_no_longer_every_module(client):
    body = _post(client, _minimal())
    enabled = set(body['enabled_indicators'])
    assert enabled != set(ALL_MODULE_KEYS)
    assert enabled == strategy_modules('ICT_MSNR_V1')
    assert len(enabled) == 25


# ─────────────────────────────────────────────
# 8. The legacy client keeps trading
# ─────────────────────────────────────────────

def test_a_legacy_client_can_still_receive_a_tradeable_signal(client):
    """The Telegram-bot path: no configuration, normal production cascade."""
    body = _post(client, _minimal())
    assert body['strategy_status'] == 'production'
    # It is NOT blocked by the production-safety gate...
    assert body['decision_reason'] not in (
        CODE_CUSTOM_STRATEGY, CODE_EXPERIMENTAL_STRATEGY
    )
    # ...so the verdict is whatever the normal gates decide.
    if body['tradeable']:
        assert body['signal'] in ('BUY', 'SELL')
        assert body['quality'] >= MIN_TRADEABLE_QUALITY
        assert body['confidence'] >= MIN_TRADEABLE_CONFIDENCE
    else:
        assert body['signal'] == 'WAIT'


def test_a_legacy_request_reaches_the_normal_gate_cascade(client):
    """Whatever the outcome, it is a real gate — not a status block."""
    from analysis.decision import (
        CODE_CONFIDENCE_BELOW, CODE_HARD_CONFLICT, CODE_MARKET_UNCLEAN,
        CODE_MIN_RR, CODE_NO_DIRECTION, CODE_NO_STOP, CODE_QUALITY_BELOW,
        CODE_RISK_CEILING, CODE_TARGETS, CODE_TP1_RR, CODE_TRADEABLE,
    )
    body = _post(client, _minimal())
    assert body['decision_reason'] in {
        CODE_TRADEABLE, CODE_MARKET_UNCLEAN, CODE_HARD_CONFLICT,
        CODE_NO_DIRECTION, CODE_QUALITY_BELOW, CODE_CONFIDENCE_BELOW,
        CODE_NO_STOP, CODE_RISK_CEILING, CODE_TARGETS, CODE_TP1_RR, CODE_MIN_RR,
    }


def test_a_legacy_client_may_publish_to_live_signals(client):
    """Live-signal eligibility follows from production status."""
    body = _post(client, _minimal())
    assert STRATEGIES[body['strategy_id']].live_signal_eligible


# ─────────────────────────────────────────────
# 4–6, 9. Explicit configuration is untouched
# ─────────────────────────────────────────────

def test_an_explicit_custom_configuration_stays_custom(client):
    body = _post(client, {
        **_minimal(), 'strategy_id': CUSTOM_STRATEGY_ID,
        'enabled_indicators': ['rsi', 'macd', 'volume'],
    })
    assert body['strategy_status'] == 'custom'
    assert body['tradeable'] is False
    assert body['signal'] == 'WAIT'
    assert body['decision_reason'] == CODE_CUSTOM_STRATEGY
    assert set(body['enabled_indicators']) == REQUIRED_MODULES | {'rsi', 'macd', 'volume'}


def test_a_module_list_without_a_strategy_id_is_still_custom(client):
    """Supplying modules IS configuring — it must not fall to the default."""
    body = _post(client, {**_minimal(), 'enabled_indicators': ['rsi', 'volume']})
    assert body['strategy_status'] == 'custom'
    assert body['tradeable'] is False
    assert set(body['enabled_indicators']) == REQUIRED_MODULES | {'rsi', 'volume'}


def test_an_empty_module_list_is_a_configuration_not_an_omission(client):
    """`[]` says "only the core modules" — a decision, not silence."""
    body = _post(client, {**_minimal(), 'enabled_indicators': []})
    assert set(body['enabled_indicators']) == REQUIRED_MODULES
    assert body['strategy_status'] == 'custom'
    assert body['tradeable'] is False


@pytest.mark.parametrize('strategy_id', EXPERIMENTAL_IDS)
def test_an_explicit_experimental_strategy_stays_experimental(client, strategy_id):
    body = _post(client, {**_minimal(), 'strategy_id': strategy_id})
    assert body['strategy_id'] == strategy_id
    assert body['strategy_status'] == 'experimental'
    assert body['tradeable'] is False
    assert body['signal'] == 'WAIT'
    assert body['decision_reason'] == CODE_EXPERIMENTAL_STRATEGY


def test_an_explicit_production_strategy_stays_production(client):
    body = _post(client, {**_minimal(), 'strategy_id': 'ICT_MSNR_V1'})
    assert body['strategy_id'] == 'ICT_MSNR_V1'
    assert body['strategy_status'] == 'production'
    assert body['decision_reason'] != CODE_CUSTOM_STRATEGY


def test_a_legacy_preset_still_resolves_to_its_own_modules(client):
    """Priority 4: a preset beats the no-config default."""
    balanced = _post(client, {**_minimal(), 'preset': 'balanced'})
    assert set(balanced['enabled_indicators']) == set(ALL_MODULE_KEYS)
    assert set(balanced['enabled_indicators']) != strategy_modules(DEFAULT_STRATEGY_ID)
    conservative = _post(client, {**_minimal(), 'preset': 'conservative'})
    assert set(conservative['enabled_indicators']) != strategy_modules(DEFAULT_STRATEGY_ID)


def test_omission_cannot_be_used_to_force_custom(client):
    """Requirement 9: the unconfigured path can never yield a CUSTOM result."""
    for payload in (
        _minimal(),
        {**_minimal(), 'strategy_id': None},
        {**_minimal(), 'preset': None},
        {**_minimal(), 'strategy_id': '', 'preset': ''},
    ):
        body = _post(client, payload)
        assert body['strategy_status'] == 'production', payload
        assert body['strategy_id'] == DEFAULT_STRATEGY_ID, payload


def test_the_resolution_priority_holds_end_to_end(client):
    """1. known id  2. unknown id  3. modules  4. preset  5. nothing."""
    # 1 — a known id wins over everything sent with it.
    assert _post(client, {
        **_minimal(), 'strategy_id': 'MOMENTUM_V1',
        'enabled_indicators': ['rsi'], 'preset': 'balanced',
    })['strategy_id'] == 'MOMENTUM_V1'
    # 2 — an unknown id is REJECTED, never substituted (that would fail open).
    assert client.post('/api/analyze', json={
        **_minimal(), 'strategy_id': 'NOPE_V9', 'enabled_indicators': ['rsi'],
    }).status_code == 400
    # 3 — modules beat a preset.
    assert set(_post(client, {
        **_minimal(), 'enabled_indicators': ['rsi'], 'preset': 'balanced',
    })['enabled_indicators']) == REQUIRED_MODULES | {'rsi'}
    # 4 — a preset beats the default.
    assert set(_post(client, {
        **_minimal(), 'preset': 'balanced',
    })['enabled_indicators']) == set(ALL_MODULE_KEYS)
    # 5 — nothing at all is the production default.
    assert _post(client, _minimal())['strategy_id'] == DEFAULT_STRATEGY_ID


# ─────────────────────────────────────────────
# 7. Nothing else moved
# ─────────────────────────────────────────────

def test_the_ict_msnr_configuration_is_unchanged(client):
    from analysis.modules import MODULE_ORDER, MODULE_WEIGHTS
    mods = strategy_modules('ICT_MSNR_V1')
    assert len(mods) == 25
    assert sum(MODULE_WEIGHTS[m] for m in MODULE_ORDER if m in mods) == 71


def test_the_thresholds_are_unchanged():
    from analysis.scoring import MIN_PROBABILITY_EDGE
    assert MIN_TRADEABLE_QUALITY == 60
    assert MIN_TRADEABLE_CONFIDENCE == 60
    assert MIN_PROBABILITY_EDGE == 10.0


def test_the_production_safety_gate_still_blocks_everything_but_ict_msnr(client):
    tradeable_ids = set()
    for strategy in STRATEGY_ORDER:
        body = _post(client, {**_minimal(), 'strategy_id': strategy.strategy_id})
        if body['tradeable']:
            tradeable_ids.add(body['strategy_id'])
    custom = _post(client, {
        **_minimal(), 'strategy_id': CUSTOM_STRATEGY_ID,
        'enabled_indicators': ['rsi', 'volume'],
    })
    assert not custom['tradeable']
    assert tradeable_ids <= {'ICT_MSNR_V1'}


def test_the_engine_library_default_is_untouched():
    """`enabled_modules=None` at the LIBRARY level still means every module.

    Only the HTTP layer treats omission as "run the production strategy";
    the pipeline's own default is unchanged, so direct callers and tests
    behave exactly as before.
    """
    from analysis.modules import resolve_enabled_modules
    assert resolve_enabled_modules(None) == frozenset(ALL_MODULE_KEYS)
