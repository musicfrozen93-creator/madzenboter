"""
ZenGrid — Fernet Encryption Service for API Key Storage.

Provides symmetric encryption for sensitive credentials (API keys,
secrets) stored in the database. Uses Fernet (AES-128-CBC + HMAC-SHA256)
from the ``cryptography`` library for authenticated encryption.

The master key must be a valid base64-encoded 32-byte Fernet key,
typically set via the MASTER_ENCRYPTION_KEY environment variable.
"""

import base64
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Sentinel value used for the init-time round-trip validation
_ROUNDTRIP_SENTINEL = 'zengrid-encryption-check'


class EncryptionError(Exception):
    """Raised when an encryption or decryption operation fails."""


class EncryptionService:
    """Fernet-based encryption service for protecting API credentials.

    Validates the master key on construction by performing a round-trip
    encrypt/decrypt cycle. All public methods raise ``EncryptionError``
    on failure so callers never see raw cryptography exceptions.

    Args:
        master_key: A base64-encoded 32-byte Fernet key string.

    Raises:
        EncryptionError: If the master key is missing, malformed, or
            fails the round-trip validation.

    Example::

        svc = EncryptionService(os.environ['MASTER_ENCRYPTION_KEY'])
        token = svc.encrypt('my-api-secret')
        assert svc.decrypt(token) == 'my-api-secret'
    """

    def __init__(self, master_key: str) -> None:
        if not master_key or not master_key.strip():
            raise EncryptionError(
                'Master encryption key must not be empty. '
                'Set the MASTER_ENCRYPTION_KEY environment variable.'
            )

        self._master_key: str = master_key.strip()

        try:
            self._fernet: Fernet = Fernet(self._master_key.encode('utf-8'))
        except (ValueError, base64.binascii.Error) as exc:
            raise EncryptionError(
                f'Invalid Fernet key format: {exc}. '
                'Generate a valid key with EncryptionService.generate_key().'
            ) from exc

        # Round-trip validation — proves the key can encrypt & decrypt
        self._validate_roundtrip()
        logger.info('EncryptionService initialised and validated successfully')

    # ───────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string and return a URL-safe base64 token.

        Args:
            plaintext: The string to encrypt (e.g. an API key).

        Returns:
            URL-safe base64-encoded ciphertext string.

        Raises:
            EncryptionError: If the plaintext is empty or encryption fails.
        """
        if not plaintext:
            raise EncryptionError('Cannot encrypt an empty string')

        try:
            token: bytes = self._fernet.encrypt(plaintext.encode('utf-8'))
            ciphertext = token.decode('utf-8')
            logger.debug(
                'Encrypted %d-char plaintext to %d-char ciphertext',
                len(plaintext), len(ciphertext),
            )
            return ciphertext
        except Exception as exc:
            raise EncryptionError(f'Encryption failed: {exc}') from exc

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a Fernet token back to the original plaintext.

        Args:
            ciphertext: URL-safe base64-encoded Fernet token.

        Returns:
            Original plaintext string.

        Raises:
            EncryptionError: If the ciphertext is empty, corrupted,
                or was encrypted with a different key.
        """
        if not ciphertext:
            raise EncryptionError('Cannot decrypt an empty ciphertext')

        try:
            plaintext_bytes: bytes = self._fernet.decrypt(
                ciphertext.encode('utf-8')
            )
            plaintext = plaintext_bytes.decode('utf-8')
            logger.debug(
                'Decrypted %d-char ciphertext to %d-char plaintext',
                len(ciphertext), len(plaintext),
            )
            return plaintext
        except InvalidToken as exc:
            raise EncryptionError(
                'Decryption failed — ciphertext is invalid or was encrypted '
                'with a different key'
            ) from exc
        except Exception as exc:
            raise EncryptionError(f'Decryption failed: {exc}') from exc

    def re_encrypt(
        self, ciphertext: str, old_service: 'EncryptionService'
    ) -> str:
        """Re-encrypt data from an old key to this service's key.

        Decrypts ``ciphertext`` using ``old_service``, then encrypts the
        resulting plaintext with *this* service's key.

        Args:
            ciphertext: Token encrypted with the old key.
            old_service: EncryptionService instance holding the old key.

        Returns:
            New ciphertext encrypted with this service's key.

        Raises:
            EncryptionError: If decryption with the old key or
                re-encryption with the new key fails.
        """
        if not isinstance(old_service, EncryptionService):
            raise EncryptionError(
                'old_service must be an EncryptionService instance'
            )

        try:
            plaintext: str = old_service.decrypt(ciphertext)
            new_ciphertext: str = self.encrypt(plaintext)
            logger.info(
                'Re-encrypted credential (%d chars) from old key to new key',
                len(plaintext),
            )
            return new_ciphertext
        except EncryptionError:
            # Already an EncryptionError — propagate as-is
            raise
        except Exception as exc:
            raise EncryptionError(
                f'Re-encryption failed: {exc}'
            ) from exc

    @staticmethod
    def mask_key(key: str) -> str:
        """Return a masked representation of a sensitive key string.

        Shows only the last 4 characters, preceded by asterisks.
        Useful for logging and admin UIs.

        Args:
            key: The sensitive string to mask (e.g. an API key).

        Returns:
            Masked string in ``'****…last4'`` format, or ``'****'``
            if the key is shorter than 5 characters.
        """
        if not key:
            return '****'
        if len(key) <= 4:
            return '****'
        return f'****{key[-4:]}'

    @staticmethod
    def generate_key() -> str:
        """Generate a new random Fernet encryption key.

        Returns:
            A URL-safe base64-encoded 32-byte key string suitable
            for use as ``MASTER_ENCRYPTION_KEY``.

        Example::

            key = EncryptionService.generate_key()
            print(f'MASTER_ENCRYPTION_KEY={key}')
        """
        key: str = Fernet.generate_key().decode('utf-8')
        logger.info('Generated new Fernet encryption key')
        return key

    # ───────────────────────────────────────────
    # Internal Helpers
    # ───────────────────────────────────────────

    def _validate_roundtrip(self) -> None:
        """Verify the master key with a round-trip encrypt/decrypt cycle.

        Raises:
            EncryptionError: If the round-trip produces a mismatch or
                any cryptographic operation fails.
        """
        try:
            token: bytes = self._fernet.encrypt(
                _ROUNDTRIP_SENTINEL.encode('utf-8')
            )
            result: str = self._fernet.decrypt(token).decode('utf-8')
        except Exception as exc:
            raise EncryptionError(
                f'Master key round-trip validation failed: {exc}'
            ) from exc

        if result != _ROUNDTRIP_SENTINEL:
            raise EncryptionError(
                'Master key round-trip validation failed — '
                'decrypted value does not match the original'
            )

    def __repr__(self) -> str:
        """Return a safe repr that never leaks the master key."""
        return (
            f'<EncryptionService key={self.mask_key(self._master_key)}>'
        )
