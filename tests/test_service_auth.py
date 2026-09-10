"""Direct-attack tests for the analysis API's service boundary.

THE THREAT
    This service is published on the public internet — Vercel's serverless
    functions are external to the VPS and cannot reach a private Docker network
    (DEPLOYMENT.md). An attacker who knows the hostname can therefore reach it
    with curl, and every control that protects the product — authentication,
    subscription, rate limiting, market entitlement — lives in the Next.js layer
    they would be skipping.

    Before this boundary existed, `curl https://api.<domain>/api/analyze -d '{...}'`
    returned real signals to anyone, with no account and no subscription.

WHAT THESE TESTS PIN
    That the service rejects everything except a caller presenting the shared
    credential, and that it fails CLOSED when misconfigured. They attack the
    service the way an outsider would: no header, junk header, a browser-style
    Origin, a near-miss key, the wrong scheme.
"""

import os

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.dependencies import get_settings
from api.security import API_KEY_ENV, MIN_KEY_LENGTH, configured_key, is_configured
from providers.registry import (
    _DEFAULT_FOR_MARKET,
    _REGISTRY,
    register,
    reset_for_tests,
)
from tests.conftest import TEST_SERVICE_KEY
from tests.fakes import StubProvider, make_uptrend

TEST_MARKET = 'authtest'


class AuthStubProvider(StubProvider):
    name = 'authstub'
    market = TEST_MARKET

    def __init__(self, settings=None):
        super().__init__(candles=make_uptrend(n=500))


@pytest.fixture
def raw_client(settings):
    """A client that presents NO credential unless a test adds one."""
    register(AuthStubProvider)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    _REGISTRY.pop(AuthStubProvider.name, None)
    _DEFAULT_FOR_MARKET.pop(TEST_MARKET, None)
    reset_for_tests()


def _payload():
    return {'market': TEST_MARKET, 'symbol': 'BTCUSDT', 'timeframe': '15m'}


PROTECTED = [
    ('POST', '/api/analyze'),
    ('GET', '/api/indicators'),
    ('GET', '/api/markets'),
]


def _call(client, method, path, **kw):
    return (client.post(path, json=_payload(), **kw) if method == 'POST'
            else client.get(path, **kw))


# ─────────────────────────────────────────────
# 1–3. Anonymous and forged requests are rejected
# ─────────────────────────────────────────────

@pytest.mark.parametrize('method,path', PROTECTED)
def test_a_direct_request_with_no_credential_is_rejected(raw_client, method, path):
    """The bare curl an attacker would run first."""
    res = _call(raw_client, method, path)
    assert res.status_code == 401, path
    assert 'credential' in res.json()['detail'].lower()


@pytest.mark.parametrize('method,path', PROTECTED)
def test_a_forged_credential_is_rejected(raw_client, method, path):
    res = _call(raw_client, method, path, headers={'X-Service-Key': 'x' * 40})
    assert res.status_code == 403, path


@pytest.mark.parametrize('bogus', [
    '',                                    # empty
    '   ',                                 # whitespace
    'null',
    'undefined',
    'Bearer',
    TEST_SERVICE_KEY[:-1],                 # near miss, one char short
    TEST_SERVICE_KEY + 'x',                # near miss, one char long
    TEST_SERVICE_KEY.upper(),              # case variation
    ' ' + TEST_SERVICE_KEY,                # leading space (stripped, so valid)
])
def test_malformed_and_near_miss_credentials(raw_client, bogus):
    res = raw_client.get('/api/indicators', headers={'X-Service-Key': bogus})
    if bogus.strip() == TEST_SERVICE_KEY:
        assert res.status_code == 200          # whitespace is trimmed
    elif not bogus.strip():
        assert res.status_code == 401          # nothing presented
    else:
        assert res.status_code == 403, bogus


def test_a_browser_origin_header_alone_grants_nothing(raw_client):
    """CORS is not authentication.

    A browser-shaped request carrying an allowed Origin — and nothing else —
    must be refused exactly like any anonymous caller.
    """
    for origin in ('http://localhost:3000', 'https://zentryai.site', 'https://evil.example'):
        res = raw_client.post(
            '/api/analyze', json=_payload(),
            headers={'Origin': origin, 'Referer': origin},
        )
        assert res.status_code == 401, origin


def test_cors_preflight_does_not_leak_data(raw_client):
    """An OPTIONS preflight may be answered, but it carries no analysis."""
    res = raw_client.options('/api/analyze', headers={
        'Origin': 'http://localhost:3000',
        'Access-Control-Request-Method': 'POST',
    })
    assert 'signal' not in res.text
    assert 'quality' not in res.text


def test_the_wrong_auth_scheme_is_rejected(raw_client):
    for header in ('Basic ' + TEST_SERVICE_KEY, TEST_SERVICE_KEY, 'Token ' + TEST_SERVICE_KEY):
        res = raw_client.get('/api/indicators', headers={'Authorization': header})
        assert res.status_code == 401, header


# ─────────────────────────────────────────────
# 4. The legitimate internal path works
# ─────────────────────────────────────────────

def test_a_valid_internal_request_succeeds(raw_client, auth_headers):
    res = raw_client.post('/api/analyze', json=_payload(), headers=auth_headers)
    assert res.status_code == 200
    assert res.json()['symbol'] == 'BTCUSDT'


def test_the_bearer_form_is_accepted(raw_client):
    """So a proxy or client that only speaks Bearer works unchanged."""
    res = raw_client.get(
        '/api/indicators',
        headers={'Authorization': f'Bearer {TEST_SERVICE_KEY}'},
    )
    assert res.status_code == 200


def test_ict_msnr_still_works_through_the_authenticated_path(raw_client, auth_headers):
    """The production strategy is unaffected by the security boundary."""
    res = raw_client.post(
        '/api/analyze',
        json={**_payload(), 'strategy_id': 'ICT_MSNR_V1'},
        headers=auth_headers,
    )
    assert res.status_code == 200
    body = res.json()
    assert body['strategy_id'] == 'ICT_MSNR_V1'
    assert body['strategy_status'] == 'production'
    assert body['analysis']['breakdown']
    assert body['decision_reason']


def test_every_protected_route_accepts_the_valid_credential(raw_client, auth_headers):
    for method, path in PROTECTED:
        res = _call(raw_client, method, path, headers=auth_headers)
        assert res.status_code == 200, path


# ─────────────────────────────────────────────
# 5–6. Fails closed when misconfigured
# ─────────────────────────────────────────────

def test_an_unconfigured_service_refuses_everyone(raw_client, monkeypatch):
    """A missing secret must NOT reopen anonymous access.

    This is the failure this whole module exists to prevent: a deploy that
    forgets the variable must break loudly, not serve the world.
    """
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    assert not is_configured()
    for method, path in PROTECTED:
        res = _call(raw_client, method, path)
        assert res.status_code == 503, path
        # And presenting any credential does not help — there is nothing to
        # compare against, so nobody is trusted.
        res = _call(raw_client, method, path, headers={'X-Service-Key': 'anything'})
        assert res.status_code == 503, path


def test_a_placeholder_length_key_is_refused(raw_client, monkeypatch):
    """A short key is a placeholder, not a credential."""
    monkeypatch.setenv(API_KEY_ENV, 'short')
    assert configured_key() is None
    assert not is_configured()
    res = raw_client.get('/api/indicators', headers={'X-Service-Key': 'short'})
    assert res.status_code == 503


def test_the_minimum_key_length_is_meaningful():
    assert MIN_KEY_LENGTH >= 16


def test_health_stays_open_for_the_container_healthcheck(raw_client, monkeypatch):
    """Docker HEALTHCHECK and uptime monitors call it unauthenticated."""
    for configured in (True, False):
        if configured:
            monkeypatch.setenv(API_KEY_ENV, TEST_SERVICE_KEY)
        else:
            monkeypatch.delenv(API_KEY_ENV, raising=False)
        res = raw_client.get('/health')
        assert res.status_code == 200
        body = res.json()
        assert body['status']
        # It discloses liveness only — no secret, no market data.
        assert API_KEY_ENV not in res.text
        assert TEST_SERVICE_KEY not in res.text


# ─────────────────────────────────────────────
# 7, 9. The secret never leaks
# ─────────────────────────────────────────────

def test_the_credential_never_appears_in_a_response(raw_client, auth_headers):
    for method, path in PROTECTED + [('GET', '/health')]:
        res = _call(raw_client, method, path, headers=auth_headers)
        assert TEST_SERVICE_KEY not in res.text, path


def test_a_rejection_does_not_disclose_the_expected_value(raw_client):
    res = raw_client.get('/api/indicators', headers={'X-Service-Key': 'wrong-but-long-enough'})
    assert res.status_code == 403
    assert TEST_SERVICE_KEY not in res.text
    assert 'expected' not in res.text.lower()


def test_the_key_is_read_from_the_environment_only():
    """No default, no fallback, nothing committed."""
    import api.security as security
    src = open(security.__file__, encoding='utf-8').read()
    assert "os.environ.get(API_KEY_ENV" in src
    # No literal that looks like a baked-in secret.
    for suspicious in ('sk-', 'secret=', 'password='):
        assert suspicious not in src.lower()


def test_the_env_var_is_not_a_next_public_name():
    """A NEXT_PUBLIC_ prefix would inline the secret into the browser bundle."""
    assert not API_KEY_ENV.startswith('NEXT_PUBLIC')
    assert API_KEY_ENV == 'ANALYSIS_API_KEY'


# ─────────────────────────────────────────────
# 8. CORS creates no authorization
# ─────────────────────────────────────────────

def test_cors_config_never_grants_access(raw_client, monkeypatch):
    """Even a permissive origin list authorises nothing."""
    monkeypatch.setenv('ALLOWED_ORIGINS', 'https://evil.example,http://localhost:3000')
    app = create_app()
    with TestClient(app) as client:
        res = client.post(
            '/api/analyze', json=_payload(),
            headers={'Origin': 'https://evil.example'},
        )
        assert res.status_code == 401


def test_a_wildcard_origin_is_dropped(monkeypatch):
    """`*` on a private engine is refused rather than honoured."""
    from api.app import _allowed_origins
    monkeypatch.setenv('ALLOWED_ORIGINS', '*,http://localhost:3000')
    origins = _allowed_origins()
    assert '*' not in origins
    assert 'http://localhost:3000' in origins
