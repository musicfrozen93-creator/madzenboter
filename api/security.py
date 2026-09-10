"""Service-to-service authentication for the analysis API.

WHY THIS EXISTS
    This service is NOT private. The website runs on Vercel, whose serverless
    functions are external to the VPS and cannot reach a Docker service name or
    a loopback address — so the analysis API has to be published on the public
    internet (see DEPLOYMENT.md). "Put it on a private network" is not available
    to us while the frontend is serverless.

    That makes the public API boundary — authentication, subscription checks,
    rate limits, market entitlements — enforceable only in the Next.js layer.
    Without a check here, anyone who knows the hostname bypasses every one of
    them with a single curl.

    CORS DOES NOT HELP. It is a browser-enforced policy: the browser declines to
    hand a cross-origin *response* to page JavaScript. curl, Postman, requests,
    and any server-side client ignore it entirely. CORS is not authentication
    and never has been.

WHAT THIS IS
    A shared secret held by the two SERVERS. Next.js sends it on every
    server-to-server call; this service compares it in constant time. The
    browser never sees it: it lives in `ANALYSIS_API_KEY`, a server-only
    environment variable on both sides, never a `NEXT_PUBLIC_*` name.

    It authenticates the CALLER SERVICE, not the end user. User identity,
    subscription and entitlement stay in Next.js, which is where the user
    session actually exists — this layer only answers "did the trusted frontend
    send this?".

IT FAILS CLOSED
    A request with no key, a malformed key, or a wrong key is rejected. So is
    every request when no key is configured at all — a missing secret must not
    silently reopen anonymous access, which is the exact failure this module
    exists to prevent. A deployment that forgets the variable breaks loudly
    instead of serving the world.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import Header, HTTPException, status

logger = logging.getLogger(__name__)

#: Server-only environment variable holding the shared secret. Deliberately not
#: prefixed NEXT_PUBLIC_ — that prefix would inline it into the browser bundle.
API_KEY_ENV = 'ANALYSIS_API_KEY'

#: The header Next.js sends it in. `Authorization: Bearer <key>` is accepted too,
#: so a reverse proxy or client that only speaks Bearer works unchanged.
SERVICE_KEY_HEADER = 'X-Service-Key'

#: Shortest secret we will accept. A key below this is a placeholder, not a
#: credential, and treating it as one would be worse than failing.
MIN_KEY_LENGTH = 16

_warned_missing = False


def configured_key() -> Optional[str]:
    """The configured shared secret, or None when it is unset/too short."""
    raw = (os.environ.get(API_KEY_ENV) or '').strip()
    if not raw:
        return None
    if len(raw) < MIN_KEY_LENGTH:
        logger.critical(
            '%s is set but is only %d characters. Refusing to treat it as a '
            'credential; requests will be rejected. Use at least %d random '
            'characters (e.g. `openssl rand -hex 32`).',
            API_KEY_ENV, len(raw), MIN_KEY_LENGTH,
        )
        return None
    return raw


def is_configured() -> bool:
    """True when a usable shared secret is present."""
    return configured_key() is not None


def _extract(x_service_key: Optional[str], authorization: Optional[str]) -> Optional[str]:
    """Pull the presented secret out of either accepted header."""
    if x_service_key and x_service_key.strip():
        return x_service_key.strip()
    if authorization:
        scheme, _, token = authorization.strip().partition(' ')
        if scheme.lower() == 'bearer' and token.strip():
            return token.strip()
    return None


def require_service_auth(
    x_service_key: Optional[str] = Header(default=None, alias=SERVICE_KEY_HEADER),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """FastAPI dependency: allow only the trusted caller service.

    Raises:
        HTTPException: 503 when the service has no key configured (it cannot
            authenticate anyone, so it serves no one); 401 when the caller
            presented nothing; 403 when the caller presented the wrong secret.
    """
    global _warned_missing

    expected = configured_key()
    if expected is None:
        # Fail CLOSED. Serving anonymously here would hand the whole analysis
        # engine — and every control the Next.js layer owns — to the internet.
        if not _warned_missing:
            _warned_missing = True
            logger.critical(
                '%s is not configured. This service is refusing every request '
                'to a protected route. Set %s to the same value on this service '
                'and on the Next.js deployment.',
                API_KEY_ENV, API_KEY_ENV,
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                'Analysis service is not configured for authenticated access. '
                f'Set {API_KEY_ENV} on the service.'
            ),
        )

    presented = _extract(x_service_key, authorization)
    if presented is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Missing service credential.',
            headers={'WWW-Authenticate': 'Bearer'},
        )

    # Constant-time: a plain `==` leaks the secret one byte at a time to anyone
    # who can measure response latency across many attempts.
    if not hmac.compare_digest(presented, expected):
        logger.warning('Rejected a request presenting an invalid service credential.')
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Invalid service credential.',
        )
