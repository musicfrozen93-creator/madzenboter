"""Contract tests for the REST API the website calls.

These lock the request/response shape the Next.js frontend depends on, assert
that no placeholder value survives, and assert the architectural guarantee that
no trading endpoint exists.
"""

import pytest
from fastapi.testclient import TestClient

from analysis.modules import MODULE_ORDER
from api.app import create_app
from api.dependencies import get_settings
from providers.registry import (
    _DEFAULT_FOR_MARKET,
    _REGISTRY,
    register,
    reset_for_tests,
)
from tests.fakes import StubProvider, make_candles, make_downtrend, make_uptrend

TEST_MARKET = 'apitest'


class ApiStubProvider(StubProvider):
    """A registered stub so /api/analyze runs without any network."""

    name = 'apistub'
    market = TEST_MARKET

    def __init__(self, settings=None):
        # The registry constructs providers with settings; the stub ignores them.
        super().__init__(candles=make_uptrend(n=500))


@pytest.fixture
def client(settings):
    """A TestClient with the stub provider registered for a throwaway market."""
    register(ApiStubProvider)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    _REGISTRY.pop(ApiStubProvider.name, None)
    _DEFAULT_FOR_MARKET.pop(TEST_MARKET, None)
    reset_for_tests()


def _analyze(client, **overrides):
    payload = {'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m'}
    payload.update(overrides)
    return client.post('/api/analyze', json=payload)


# ─────────────────────────────────────────────
# Health & discovery
# ─────────────────────────────────────────────

def test_health_reports_ok(client):
    body = client.get('/health').json()
    assert body['status'] == 'ok'
    assert body['service'] == 'zentry-analysis-api'


def test_health_asserts_trading_is_impossible(client):
    """Architectural guarantee: this service can never place an order."""
    assert client.get('/health').json()['trading_enabled'] is False


def test_health_exposes_cache_statistics(client):
    assert 'hit_rate' in client.get('/health').json()['cache']


def test_markets_lists_providers_without_network(client):
    body = client.get('/api/markets').json()
    assert 'crypto' in body['markets']
    binance = next(p for p in body['providers'] if p['provider'] == 'binance')
    assert binance['is_default'] is True
    assert '15m' in binance['timeframes']


def test_symbols_endpoint_lists_a_markets_pairs(client):
    body = client.get(f'/api/markets/{TEST_MARKET}/symbols').json()
    assert body['provider'] == 'apistub'
    assert 'BTCUSDT' in body['symbols']
    assert body['count'] == len(body['symbols'])


def test_unknown_market_symbols_is_a_400(client):
    assert client.get('/api/markets/commodities/symbols').status_code == 400


# ─────────────────────────────────────────────
# POST /api/analyze — the contract
# ─────────────────────────────────────────────

def test_analyze_returns_the_documented_shape(client):
    res = _analyze(client)
    assert res.status_code == 200
    body = res.json()

    for key in (
        'signal', 'quality', 'confidence', 'entry', 'sl', 'tp',
        'analysis', 'reasons', 'headline',
    ):
        assert key in body, key

    assert body['signal'] in ('BUY', 'SELL', 'WAIT')
    assert body['symbol'] == 'BTCUSDT'
    assert body['timeframe'] == '15m'
    assert body['market'] == TEST_MARKET
    assert isinstance(body['tp'], list)
    assert body['generated_at']


def test_no_placeholder_values_remain(client):
    """Phase 1's headline requirement: quality and confidence are never null."""
    body = _analyze(client).json()
    assert isinstance(body['quality'], int)
    assert isinstance(body['confidence'], int)
    assert 0 <= body['quality'] <= 100
    assert 0 <= body['confidence'] <= 100
    assert body['quality_grade']
    assert body['confidence_grade']
    assert 'pending' not in body
    assert 'ai_review' not in body


def test_analyze_includes_the_full_module_breakdown(client):
    analysis = _analyze(client).json()['analysis']
    breakdown = {row['module']: row for row in analysis['breakdown']}

    assert set(breakdown) == set(MODULE_ORDER)
    for row in breakdown.values():
        assert row['label']
        assert row['direction'] in ('bullish', 'bearish', 'neutral')
        assert 0 <= row['score'] <= row['max_score']


def test_analyze_includes_every_module_result(client):
    analysis = _analyze(client).json()['analysis']

    assert analysis['trend'] in ('bullish', 'bearish', 'range')
    assert analysis['structure']['trend']
    assert analysis['elliott']['current_wave']
    assert 'in_golden_pocket' in analysis['fibonacci']
    assert 'position' in analysis['levels']
    assert 'rsi' in analysis['indicators']
    assert analysis['confluence']['direction']
    assert len(analysis['market_quality']) == 4
    assert analysis['candles_analyzed'] > 0


# ─────────────────────────────────────────────
# Phase 2 — Smart Money fields (additive, backward-compatible)
# ─────────────────────────────────────────────

def test_response_still_carries_every_phase1_field(client):
    """Phase 2 must ADD fields, never remove the Phase 1 contract."""
    body = _analyze(client).json()
    for key in (
        'signal', 'quality', 'quality_grade', 'confidence', 'confidence_grade',
        'entry', 'sl', 'tp', 'headline', 'reasons', 'analysis',
        'quality_detail', 'confidence_detail', 'generated_at',
    ):
        assert key in body, f'Phase 1 field {key} disappeared'
    analysis = body['analysis']
    for key in ('trend', 'structure', 'elliott', 'fibonacci', 'levels',
                'indicators', 'regime', 'confluence', 'breakdown'):
        assert key in analysis, f'Phase 1 analysis field {key} disappeared'


def test_all_seven_smc_blocks_are_present(client):
    analysis = _analyze(client).json()['analysis']
    for block in ('order_blocks', 'fair_value_gaps', 'liquidity',
                  'vwap', 'macd', 'adx', 'patterns'):
        assert block in analysis, block


def test_smc_blocks_have_directions_and_scores(client):
    analysis = _analyze(client).json()['analysis']
    for block in ('order_blocks', 'fair_value_gaps', 'liquidity',
                  'vwap', 'macd', 'adx', 'patterns'):
        data = analysis[block]
        assert data['direction'] in ('bullish', 'bearish', 'neutral')
        assert 0.0 <= data['score'] <= 1.0


def test_breakdown_now_lists_fifteen_modules(client):
    from analysis.modules import MODULE_ORDER
    breakdown = _analyze(client).json()['analysis']['breakdown']
    assert len(breakdown) == 15
    assert {row['module'] for row in breakdown} == set(MODULE_ORDER)


def test_smc_specific_fields_are_typed_correctly(client):
    analysis = _analyze(client).json()['analysis']
    assert isinstance(analysis['liquidity']['equal_highs'], int)
    assert isinstance(analysis['liquidity']['equal_lows'], int)
    assert analysis['adx']['trend_strength'] in ('strong', 'weak', 'no_trend')
    assert isinstance(analysis['macd']['bullish_cross'], bool)
    assert 'name' in analysis['patterns']
    assert 'above' in analysis['vwap']


# ─────────────────────────────────────────────
# Phase 3 — Trade Intelligence (additive, backward-compatible)
# ─────────────────────────────────────────────

def test_response_still_carries_phase1_and_phase2_fields(client):
    """Phase 3 must ADD an intelligence block, never touch the earlier contract."""
    body = _analyze(client).json()
    for key in ('signal', 'quality', 'confidence', 'entry', 'sl', 'tp',
                'headline', 'reasons', 'analysis', 'quality_detail'):
        assert key in body
    # Phase 2 SMC blocks still present.
    for block in ('order_blocks', 'fair_value_gaps', 'liquidity', 'vwap',
                  'macd', 'adx', 'patterns'):
        assert block in body['analysis']


def test_intelligence_block_is_present(client):
    body = _analyze(client).json()
    assert 'intelligence' in body
    it = body['intelligence']
    for section in ('validation', 'health', 'trade_guide', 'lifecycle',
                    'risk', 'invalidation', 'explanation'):
        assert section in it, section


def test_validation_block_shape(client):
    val = _analyze(client).json()['intelligence']['validation']
    assert 0 <= val['score'] <= 100
    assert 'Validation' in val['status']
    assert len(val['checks']) == 15
    for check in val['checks']:
        assert check['status'] in ('confirmed', 'neutral', 'against')


def test_health_block_shape(client):
    health = _analyze(client).json()['intelligence']['health']
    assert 1 <= health['stars'] <= 5
    assert health['stars_display'].count('★') == health['stars']
    assert health['label'] in ('Excellent', 'Good', 'Average', 'Weak', 'Poor')


def test_lifecycle_and_guide_shape(client):
    it = _analyze(client).json()['intelligence']
    assert it['lifecycle']['status'] in ('waiting', 'triggered', 'no_setup')
    assert it['lifecycle']['expiration']
    assert len(it['lifecycle']['stages']) == 9        # the full execution timeline
    assert isinstance(it['trade_guide']['steps'], list)


def test_risk_and_invalidation_shape(client):
    it = _analyze(client).json()['intelligence']
    assert it['risk']['level'] in ('Low', 'Medium', 'High')
    assert it['invalidation']['conditions']
    for cond in it['invalidation']['conditions']:
        assert cond['source'] and cond['condition']


def test_explanation_has_both_modes(client):
    exp = _analyze(client).json()['intelligence']['explanation']
    assert exp['summary']
    assert isinstance(exp['beginner'], list)
    assert isinstance(exp['professional'], list)


def test_intelligence_does_not_alter_the_signal(client):
    """The signal fields are identical to what the engine produced."""
    body = _analyze(client).json()
    # The intelligence layer reports the SAME direction; it never overrides it.
    assert body['intelligence']['trade_guide']['tradeable'] == (body['signal'] in ('BUY', 'SELL'))


def test_score_breakdowns_are_returned(client):
    body = _analyze(client).json()
    assert body['quality_detail']['value'] == body['quality']
    assert body['confidence_detail']['value'] == body['confidence']
    assert len(body['quality_detail']['components']) == len(MODULE_ORDER)
    assert body['confidence_detail']['components']


def test_elliott_always_offers_an_alternative_count(client):
    elliott = _analyze(client).json()['analysis']['elliott']
    assert elliott['current_wave']
    assert 0 <= elliott['confidence'] <= 100
    assert 0 <= elliott['completion_pct'] <= 100
    if elliott['primary']:
        assert elliott['alternative'] is not None


def test_internal_timeframes_are_never_exposed(client):
    """The user selected one timeframe and must only see that one."""
    body = _analyze(client).json()
    assert body['timeframe'] == '15m'

    # Only the count is revealed — never the internal rungs themselves.
    assert body['analysis']['timeframes_analyzed'] == 3
    serialized = str(body)
    for internal in ('"4h"', "'4h'", '"1h"', "'1h'"):
        assert internal not in serialized, f'internal timeframe {internal} leaked'


def test_wait_is_a_200_not_an_error(client):
    body = _analyze(client).json()
    if body['signal'] == 'WAIT':
        assert body['wait_reason']
        assert body['tp'] == []
        assert body['entry'] is None
        # Scores are still computed and reported for a WAIT.
        assert isinstance(body['quality'], int)


def test_an_actionable_signal_has_a_complete_ladder(client):
    body = _analyze(client).json()
    if body['signal'] in ('BUY', 'SELL'):
        assert body['entry'] and body['sl']
        assert len(body['tp']) == 3
        assert body['risk_reward'] > 0
        assert len(body['target_sources']) == 3
        assert body['entry_basis'] and body['stop_basis']


def test_reasons_are_human_readable(client):
    body = _analyze(client).json()
    assert body['reasons']
    for line in body['reasons']:
        assert line[0] in ('✓', '✕', '•')
    assert body['headline']


# ─────────────────────────────────────────────
# Invalid input
# ─────────────────────────────────────────────

def test_symbol_is_normalized_before_reaching_the_provider(client):
    res = _analyze(client, symbol='btcusdt')
    assert res.status_code == 200
    assert res.json()['symbol'] == 'BTCUSDT'


@pytest.mark.parametrize('symbol', ['!!!', 'B', 'DROP TABLE', ''])
def test_malformed_symbol_is_rejected(client, symbol):
    assert _analyze(client, symbol=symbol).status_code in (400, 422)


def test_unknown_symbol_is_a_400(client):
    assert _analyze(client, symbol='NOTLISTED').status_code == 400


@pytest.mark.parametrize('timeframe', ['7m', '2d', 'daily'])
def test_unknown_timeframe_is_a_400(client, timeframe):
    assert _analyze(client, timeframe=timeframe).status_code == 400


def test_timeframe_the_provider_does_not_serve_is_a_400(client):
    # '3d' is canonical but outside ApiStubProvider.timeframes.
    assert _analyze(client, timeframe='3d').status_code == 400


def test_unknown_market_is_a_400(client):
    assert _analyze(client, market='commodities').status_code == 400


def test_provider_from_another_market_is_a_400(client):
    assert _analyze(client, market='crypto', provider='apistub').status_code == 400


def test_missing_required_fields_is_a_422(client):
    assert client.post('/api/analyze', json={'market': TEST_MARKET}).status_code == 422


def test_insufficient_history_is_a_422(client):
    """A pair with too little history is unprocessable, not a server error."""

    class ThinProvider(ApiStubProvider):
        name = 'thinstub'

        def __init__(self, settings=None):
            StubProvider.__init__(self, candles=make_candles(n=40))

    try:
        register(ThinProvider, default_for_market=True)
        res = client.post('/api/analyze', json={
            'market': TEST_MARKET, 'symbol': 'BTCUSDT',
            'timeframe': '15m', 'provider': 'thinstub',
        })
        assert res.status_code == 422
    finally:
        _REGISTRY.pop('thinstub', None)
        _DEFAULT_FOR_MARKET[TEST_MARKET] = ApiStubProvider.name
        reset_for_tests()


# ─────────────────────────────────────────────
# Architectural guarantees
# ─────────────────────────────────────────────

@pytest.mark.parametrize('path', [
    '/api/trade', '/api/order', '/api/orders', '/api/positions',
    '/api/execute', '/api/close', '/api/accounts', '/api/balance',
])
def test_no_trading_endpoint_exists(client, path):
    """The service must never grow an order path."""
    assert client.post(path, json={}).status_code == 404
    assert client.get(path).status_code == 404


def test_route_table_contains_only_analysis_endpoints(client):
    paths = {route.path for route in client.app.routes}
    expected = {'/health', '/api/analyze', '/api/markets', '/api/markets/{market}/symbols'}
    assert expected.issubset(paths)

    forbidden = {'order', 'trade', 'position', 'execute', 'balance', 'withdraw'}
    for path in paths:
        assert not any(word in path.lower() for word in forbidden), path


def test_repeated_analysis_is_deterministic(client):
    first = _analyze(client).json()
    second = _analyze(client).json()
    assert first['signal'] == second['signal']
    assert first['quality'] == second['quality']
    assert first['confidence'] == second['confidence']
    assert first['tp'] == second['tp']
