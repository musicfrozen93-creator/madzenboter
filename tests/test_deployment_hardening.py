"""Deployment-surface regression tests.

These pin the INFRASTRUCTURE decisions, not the engine:

  * the interactive docs are off in production and on in development;
  * /health stays open so the Docker HEALTHCHECK keeps working;
  * the analysis routes stay behind the service credential;
  * the compose file keeps PostgreSQL's exposure DOCUMENTED, because it is a
    verified requirement rather than an oversight — Vercel queries the database
    directly at runtime and migrates against it at build time, and a serverless
    function cannot use the internal Docker network;
  * the analysis service itself never needs the host port for the database.

They read the repository, so they catch a regression in configuration. They
cannot tell you what is actually running on the VPS.
"""

import io
import os
import re

import pytest
from fastapi.testclient import TestClient

from api.app import _docs_enabled, create_app
from api.security import API_KEY_ENV
from tests.conftest import TEST_SERVICE_KEY

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = os.path.join(REPO, 'docker-compose.yml')
DOCKERFILE = os.path.join(REPO, 'Dockerfile')


def _read(path):
    return io.open(path, encoding='utf-8').read()


@pytest.fixture
def clean_env(monkeypatch):
    for var in ('ENVIRONMENT', 'ENV', 'NODE_ENV', 'ENABLE_DOCS'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(API_KEY_ENV, TEST_SERVICE_KEY)
    return monkeypatch


# ─────────────────────────────────────────────
# 4–5. Interactive docs
# ─────────────────────────────────────────────

@pytest.mark.parametrize('env_var', ['ENVIRONMENT', 'ENV', 'NODE_ENV'])
def test_docs_are_disabled_in_production(clean_env, env_var):
    clean_env.setenv(env_var, 'production')
    assert _docs_enabled() is False
    with TestClient(create_app()) as client:
        for path in ('/docs', '/redoc', '/openapi.json'):
            # 404, not an empty page — the route is not mounted at all.
            assert client.get(path).status_code == 404, path


def test_docs_are_available_in_development(clean_env):
    clean_env.setenv('ENVIRONMENT', 'development')
    assert _docs_enabled() is True
    with TestClient(create_app()) as client:
        for path in ('/docs', '/redoc', '/openapi.json'):
            assert client.get(path).status_code == 200, path


def test_docs_default_to_enabled_when_no_environment_is_set(clean_env):
    """A bare local run stays convenient."""
    assert _docs_enabled() is True


@pytest.mark.parametrize('value', ['prod', 'production', 'PRODUCTION', 'Prod'])
def test_any_prod_spelling_disables_docs(clean_env, value):
    clean_env.setenv('ENVIRONMENT', value)
    assert _docs_enabled() is False


def test_docs_can_be_forced_either_way(clean_env):
    """A staging box may want them on; anywhere may want them off."""
    clean_env.setenv('ENVIRONMENT', 'production')
    clean_env.setenv('ENABLE_DOCS', 'true')
    assert _docs_enabled() is True
    clean_env.setenv('ENVIRONMENT', 'development')
    clean_env.setenv('ENABLE_DOCS', 'false')
    assert _docs_enabled() is False


# ─────────────────────────────────────────────
# 2. The container healthcheck keeps working
# ─────────────────────────────────────────────

def test_health_is_reachable_in_production_without_a_credential(clean_env):
    """The Docker HEALTHCHECK calls it anonymously; it must not 401 or 404."""
    clean_env.setenv('ENVIRONMENT', 'production')
    with TestClient(create_app()) as client:
        res = client.get('/health')
        assert res.status_code == 200
        assert res.json()['status']


def test_health_survives_a_missing_credential(clean_env):
    """A container with no key must still report liveness, or it restart-loops."""
    clean_env.delenv(API_KEY_ENV, raising=False)
    clean_env.setenv('ENVIRONMENT', 'production')
    with TestClient(create_app()) as client:
        assert client.get('/health').status_code == 200


def test_the_dockerfile_healthcheck_targets_the_open_endpoint():
    src = _read(DOCKERFILE)
    healthcheck = src[src.index('HEALTHCHECK'):]
    assert '/health' in healthcheck
    # It must not point at a route that now requires a credential.
    for protected in ('/api/analyze', '/api/markets', '/api/indicators', '/docs'):
        assert protected not in healthcheck


# ─────────────────────────────────────────────
# 3. Protected routes stay protected
# ─────────────────────────────────────────────

@pytest.mark.parametrize('path', ['/api/indicators', '/api/markets'])
def test_protected_routes_still_require_the_credential_in_production(clean_env, path):
    clean_env.setenv('ENVIRONMENT', 'production')
    with TestClient(create_app()) as client:
        assert client.get(path).status_code == 401
        assert client.get(
            path, headers={'X-Service-Key': TEST_SERVICE_KEY}
        ).status_code == 200


def test_disabling_docs_does_not_disable_authentication(clean_env):
    """The two controls are independent."""
    clean_env.setenv('ENVIRONMENT', 'production')
    with TestClient(create_app()) as client:
        assert client.get('/docs').status_code == 404
        assert client.get('/api/indicators').status_code == 401


# ─────────────────────────────────────────────
# 1. PostgreSQL exposure is deliberate and documented
# ─────────────────────────────────────────────

def test_the_analysis_service_reaches_postgres_over_the_internal_network():
    """It uses the Docker service name, so IT never needs the host port."""
    src = _read(COMPOSE)
    assert '@postgres:5432/zengrid' in src
    assert 'DATABASE_URL=postgresql://zengrid:' in src


def test_the_postgres_host_publish_is_justified_in_writing():
    """It is public by necessity — that reasoning must stay next to the config.

    If someone later removes the host publish believing it unused, Vercel's
    runtime queries and its build-time migration both break.
    """
    src = _read(COMPOSE)
    block = src[src.index('PUBLIC BY NECESSITY'):src.index('- "5432:5432"')]
    for required in ('Vercel', 'migrate-on-build', 'internal Docker network'):
        assert required in block, required
    # And it must carry the mitigations, not just the reason.
    for mitigation in ('Firewall', 'TLS', 'POSTGRES_PASSWORD'):
        assert mitigation in block, mitigation


def test_no_service_publishes_a_port_without_explanation():
    """Every host publish in the compose file is commented."""
    src = _read(COMPOSE)
    published = re.findall(r'^\s*-\s*"([^"]+:\d+)"\s*$', src, re.M)
    # Exactly two, both documented: postgres 5432 and the analysis API.
    assert len(published) == 2, published
    assert any('5432' in p for p in published)
    assert any('8000' in p for p in published)


def test_the_analysis_api_key_is_required_by_the_compose_file():
    """`${VAR:?msg}` makes the stack refuse to start without it — no default."""
    src = _read(COMPOSE)
    assert 'ANALYSIS_API_KEY=${ANALYSIS_API_KEY:?' in src
    # No default value, no fallback, no committed secret.
    assert 'ANALYSIS_API_KEY=${ANALYSIS_API_KEY:-' not in src
    assert not re.search(r'ANALYSIS_API_KEY=[A-Za-z0-9]{8,}', src)


def test_the_compose_file_contains_no_committed_secret():
    src = _read(COMPOSE)
    # Passwords come from the environment; the only literal is a dev default.
    for line in src.splitlines():
        if 'PASSWORD' in line and '${' not in line and not line.strip().startswith('#'):
            pytest.fail(f'literal password in compose: {line.strip()[:60]}')


# ─────────────────────────────────────────────
# CORS remains non-authoritative
# ─────────────────────────────────────────────

def test_cors_is_documented_as_not_being_authentication():
    src = _read(os.path.join(REPO, 'api', 'app.py'))
    assert 'CORS IS NOT AUTHENTICATION' in src


def test_the_deployment_doc_states_the_security_boundary():
    doc = _read(os.path.join(REPO, 'DEPLOYMENT.md'))
    for required in (
        'Security boundary',
        'ANALYSIS_API_KEY',
        'CORS is not authentication',
        'fails closed',
    ):
        assert required in doc, required
