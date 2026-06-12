"""ZenGrid — Account Manager.

CRUD operations for trading accounts with encrypted API key storage,
balance/position sync, and credential validation.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from accounts.encryption import EncryptionService
from config.settings import Settings
from core.database import Database
from core.models import AccountModel, UserModel
from exchange.client import ExchangeClient

logger = logging.getLogger(__name__)


class AccountManager:
    """Manages trading account lifecycle — creation, updates, credential
    encryption, balance syncing, and position retrieval.

    All database operations use ``self.db.session()`` for automatic
    commit/rollback semantics.  API keys are Fernet-encrypted at rest
    via ``EncryptionService`` and only decrypted in-memory when an
    ``ExchangeClient`` needs to be created.

    Args:
        db: Database repository instance.
        encryption: EncryptionService for API key encryption/decryption.
        settings: Global application settings.
    """

    def __init__(
        self,
        db: Database,
        encryption: EncryptionService,
        settings: Settings,
    ) -> None:
        self.db = db
        self.encryption = encryption
        self.settings = settings
        logger.info('AccountManager initialised')

    # ───────────────────────────────────────────
    # Create
    # ───────────────────────────────────────────

    def create_account(
        self,
        user_id: int,
        label: str,
        api_key: str,
        api_secret: str,
        **kwargs,
    ) -> AccountModel:
        """Create a new trading account with encrypted credentials.

        Encrypts the API key and secret, validates them against the
        exchange by performing a test balance fetch, then persists the
        account to the database.

        Args:
            user_id: Owning user's ID.
            label: Human-readable account label.
            api_key: Plaintext Binance API key.
            api_secret: Plaintext Binance API secret.
            **kwargs: Optional overrides — ``is_active``, ``use_testnet``,
                ``risk_pct``, ``max_positions``, ``leverage_override``,
                ``tp_settings``, ``sl_settings``.

        Returns:
            The newly created AccountModel instance.

        Raises:
            ValueError: If the user does not exist or credentials are
                invalid (test balance fetch fails).
            RuntimeError: If database persistence fails.
        """
        use_testnet = kwargs.get('use_testnet', False)

        # Validate credentials before persisting
        if not self.validate_api_credentials(api_key, api_secret, use_testnet=use_testnet):
            raise ValueError(
                f'API credential validation failed for label={label!r}. '
                'Unable to connect to the exchange with the provided keys.'
            )

        # Encrypt keys
        encrypted_key = self.encryption.encrypt(api_key)
        encrypted_secret = self.encryption.encrypt(api_secret)

        try:
            with self.db.session() as session:
                # Verify user exists
                user = session.get(UserModel, user_id)
                if user is None:
                    raise ValueError(f'User with id={user_id} does not exist')

                account = AccountModel(
                    user_id=user_id,
                    label=label,
                    encrypted_api_key=encrypted_key,
                    encrypted_api_secret=encrypted_secret,
                    is_active=kwargs.get('is_active', True),
                    use_testnet=use_testnet,
                    risk_pct=kwargs.get('risk_pct', 0.02),
                    max_positions=kwargs.get('max_positions', 8),
                    leverage_override=kwargs.get('leverage_override'),
                    tp_settings=kwargs.get('tp_settings'),
                    sl_settings=kwargs.get('sl_settings'),
                )
                session.add(account)
                session.flush()  # Populate account.id before commit

                logger.info(
                    'Account created: id=%d label=%r user_id=%d testnet=%s',
                    account.id, label, user_id, use_testnet,
                )
                return account

        except ValueError:
            raise
        except Exception as exc:
            logger.error(
                'Failed to create account label=%r for user_id=%d: %s',
                label, user_id, exc,
            )
            raise RuntimeError(
                f'Failed to create account: {exc}'
            ) from exc

    # ───────────────────────────────────────────
    # Update
    # ───────────────────────────────────────────

    def update_account(
        self, account_id: int, **kwargs
    ) -> Optional[AccountModel]:
        """Update an existing account's fields.

        If ``api_key`` or ``api_secret`` are provided they are
        re-encrypted before storage.

        Args:
            account_id: Primary key of the account to update.
            **kwargs: Fields to update — ``label``, ``api_key``,
                ``api_secret``, ``is_active``, ``use_testnet``,
                ``risk_pct``, ``max_positions``, ``leverage_override``,
                ``tp_settings``, ``sl_settings``.

        Returns:
            Updated AccountModel, or None if the account was not found.
        """
        try:
            with self.db.session() as session:
                account = session.get(AccountModel, account_id)
                if account is None:
                    logger.warning(
                        'Account id=%d not found for update', account_id,
                    )
                    return None

                # Simple field updates
                simple_fields = (
                    'label', 'is_active', 'use_testnet', 'risk_pct',
                    'max_positions', 'leverage_override',
                    'tp_settings', 'sl_settings',
                )
                for field_name in simple_fields:
                    if field_name in kwargs and kwargs[field_name] is not None:
                        setattr(account, field_name, kwargs[field_name])

                # Re-encrypt API key if provided
                new_api_key = kwargs.get('api_key')
                if new_api_key is not None:
                    account.encrypted_api_key = self.encryption.encrypt(
                        new_api_key
                    )
                    logger.info(
                        'API key re-encrypted for account id=%d', account_id,
                    )

                # Re-encrypt API secret if provided
                new_api_secret = kwargs.get('api_secret')
                if new_api_secret is not None:
                    account.encrypted_api_secret = self.encryption.encrypt(
                        new_api_secret
                    )
                    logger.info(
                        'API secret re-encrypted for account id=%d',
                        account_id,
                    )

                session.flush()
                logger.info(
                    'Account updated: id=%d fields=%s',
                    account_id, list(kwargs.keys()),
                )
                return account

        except Exception as exc:
            logger.error(
                'Failed to update account id=%d: %s', account_id, exc,
            )
            raise

    # ───────────────────────────────────────────
    # Delete
    # ───────────────────────────────────────────

    def delete_account(
        self, account_id: int, hard: bool = False
    ) -> bool:
        """Delete a trading account.

        By default performs a soft-delete (sets ``is_active=False``).
        Pass ``hard=True`` to permanently remove the record.

        Args:
            account_id: Primary key of the account.
            hard: If True, permanently delete the row from the database.

        Returns:
            True if the account was found and deleted/deactivated,
            False if no account with that ID exists.
        """
        try:
            with self.db.session() as session:
                account = session.get(AccountModel, account_id)
                if account is None:
                    logger.warning(
                        'Account id=%d not found for deletion', account_id,
                    )
                    return False

                if hard:
                    session.delete(account)
                    logger.info(
                        'Account hard-deleted: id=%d label=%r',
                        account_id, account.label,
                    )
                else:
                    account.is_active = False
                    logger.info(
                        'Account soft-deleted (deactivated): id=%d label=%r',
                        account_id, account.label,
                    )

                return True

        except Exception as exc:
            logger.error(
                'Failed to delete account id=%d: %s', account_id, exc,
            )
            raise

    # ───────────────────────────────────────────
    # Enable / Disable
    # ───────────────────────────────────────────

    def enable_account(
        self, account_id: int
    ) -> Optional[AccountModel]:
        """Activate a previously disabled account.

        Args:
            account_id: Primary key of the account.

        Returns:
            Updated AccountModel, or None if not found.
        """
        try:
            with self.db.session() as session:
                account = session.get(AccountModel, account_id)
                if account is None:
                    logger.warning(
                        'Account id=%d not found for enable', account_id,
                    )
                    return None

                account.is_active = True
                session.flush()
                logger.info('Account enabled: id=%d', account_id)
                return account

        except Exception as exc:
            logger.error(
                'Failed to enable account id=%d: %s', account_id, exc,
            )
            raise

    def disable_account(
        self, account_id: int
    ) -> Optional[AccountModel]:
        """Deactivate an account (stops it from trading).

        Args:
            account_id: Primary key of the account.

        Returns:
            Updated AccountModel, or None if not found.
        """
        try:
            with self.db.session() as session:
                account = session.get(AccountModel, account_id)
                if account is None:
                    logger.warning(
                        'Account id=%d not found for disable', account_id,
                    )
                    return None

                account.is_active = False
                session.flush()
                logger.info('Account disabled: id=%d', account_id)
                return account

        except Exception as exc:
            logger.error(
                'Failed to disable account id=%d: %s', account_id, exc,
            )
            raise

    # ───────────────────────────────────────────
    # Balance & Position Sync
    # ───────────────────────────────────────────

    def get_account_balance(self, account_id: int) -> dict:
        """Fetch the live USDT balance for an account from the exchange.

        Creates a temporary ``ExchangeClient`` with decrypted keys,
        retrieves the balance, and updates the account's
        ``cached_balance`` and ``last_sync_at`` fields in the database.

        Args:
            account_id: Primary key of the account.

        Returns:
            Dict with keys ``total``, ``free``, ``used`` (floats in USDT).

        Raises:
            ValueError: If the account does not exist.
            RuntimeError: If the exchange balance fetch fails.
        """
        try:
            with self.db.session() as session:
                account = session.get(AccountModel, account_id)
                if account is None:
                    raise ValueError(
                        f'Account id={account_id} not found'
                    )

                api_key, api_secret = self.decrypt_account_keys(account)

                # Build a temporary exchange client
                account_settings = Settings.create_account_settings(
                    self.settings,
                    {
                        'risk_pct': account.risk_pct,
                        'max_positions': account.max_positions,
                        'leverage_override': account.leverage_override,
                        'tp_settings': account.tp_settings,
                        'sl_settings': account.sl_settings,
                    },
                )
                account_settings.use_testnet = account.use_testnet

                client = ExchangeClient.for_account(
                    account_settings, api_key, api_secret,
                )
                client.initialize()
                balance = client.fetch_balance()

                # Update cached balance
                account.cached_balance = balance.get('total', 0.0)
                account.last_sync_at = datetime.now(timezone.utc)
                session.flush()

                logger.info(
                    'Balance synced for account id=%d: total=%.4f free=%.4f used=%.4f',
                    account_id,
                    balance.get('total', 0.0),
                    balance.get('free', 0.0),
                    balance.get('used', 0.0),
                )
                return balance

        except ValueError:
            raise
        except Exception as exc:
            logger.error(
                'Failed to fetch balance for account id=%d: %s',
                account_id, exc,
            )
            raise RuntimeError(
                f'Balance fetch failed for account id={account_id}: {exc}'
            ) from exc

    def sync_account_positions(self, account_id: int) -> list:
        """Fetch open positions from the exchange and store them in the DB.

        Retrieves all open futures positions for the account, updates
        existing position records, and inserts new ones.

        Args:
            account_id: Primary key of the account.

        Returns:
            List of position dicts as returned by ``ExchangeClient.fetch_positions()``.

        Raises:
            ValueError: If the account does not exist.
            RuntimeError: If the exchange position fetch fails.
        """
        from core.models import PositionModel

        try:
            with self.db.session() as session:
                account = session.get(AccountModel, account_id)
                if account is None:
                    raise ValueError(
                        f'Account id={account_id} not found'
                    )

                api_key, api_secret = self.decrypt_account_keys(account)

                account_settings = Settings.create_account_settings(
                    self.settings,
                    {
                        'risk_pct': account.risk_pct,
                        'max_positions': account.max_positions,
                        'leverage_override': account.leverage_override,
                        'tp_settings': account.tp_settings,
                        'sl_settings': account.sl_settings,
                    },
                )
                account_settings.use_testnet = account.use_testnet

                client = ExchangeClient.for_account(
                    account_settings, api_key, api_secret,
                )
                client.initialize()
                exchange_positions = client.fetch_positions()

                # Mark all current open positions as closed first,
                # then re-open the ones that are still active on the exchange
                session.query(PositionModel).filter(
                    PositionModel.account_id == account_id,
                    PositionModel.status == 'open',
                ).update({'status': 'closed', 'closed_at': datetime.now(timezone.utc)})

                for pos in exchange_positions:
                    # Check if a matching position already exists
                    existing = (
                        session.query(PositionModel)
                        .filter(
                            PositionModel.account_id == account_id,
                            PositionModel.symbol == pos['symbol'],
                            PositionModel.side == pos['side'],
                        )
                        .first()
                    )

                    if existing:
                        existing.status = 'open'
                        existing.quantity = pos['contracts']
                        existing.entry_price = pos['entryPrice']
                        existing.unrealized_pnl = pos['unrealizedPnl']
                        existing.leverage = pos['leverage']
                        existing.liquidation_price = pos['liquidationPrice']
                        existing.closed_at = None
                        existing.updated_at = datetime.now(timezone.utc)
                    else:
                        new_position = PositionModel(
                            account_id=account_id,
                            symbol=pos['symbol'],
                            side=pos['side'],
                            quantity=pos['contracts'],
                            entry_price=pos['entryPrice'],
                            unrealized_pnl=pos['unrealizedPnl'],
                            leverage=pos['leverage'],
                            liquidation_price=pos['liquidationPrice'],
                            status='open',
                        )
                        session.add(new_position)

                # Update last sync timestamp on the account
                account.last_sync_at = datetime.now(timezone.utc)
                session.flush()

                logger.info(
                    'Positions synced for account id=%d: %d open positions',
                    account_id, len(exchange_positions),
                )
                return exchange_positions

        except ValueError:
            raise
        except Exception as exc:
            logger.error(
                'Failed to sync positions for account id=%d: %s',
                account_id, exc,
            )
            raise RuntimeError(
                f'Position sync failed for account id={account_id}: {exc}'
            ) from exc

    # ───────────────────────────────────────────
    # Queries
    # ───────────────────────────────────────────

    def get_active_accounts(self) -> List[AccountModel]:
        """Fetch all active trading accounts.

        Returns:
            List of AccountModel instances where ``is_active`` is True.
        """
        try:
            with self.db.session() as session:
                accounts = (
                    session.query(AccountModel)
                    .filter(AccountModel.is_active.is_(True))
                    .order_by(AccountModel.id)
                    .all()
                )
                logger.debug('Fetched %d active accounts', len(accounts))
                return accounts
        except Exception as exc:
            logger.error('Failed to fetch active accounts: %s', exc)
            raise

    def get_all_accounts(self) -> List[AccountModel]:
        """Fetch all trading accounts (active and inactive).

        Returns:
            List of all AccountModel instances ordered by ID.
        """
        try:
            with self.db.session() as session:
                accounts = (
                    session.query(AccountModel)
                    .order_by(AccountModel.id)
                    .all()
                )
                logger.debug('Fetched %d total accounts', len(accounts))
                return accounts
        except Exception as exc:
            logger.error('Failed to fetch all accounts: %s', exc)
            raise

    def get_account(self, account_id: int) -> Optional[AccountModel]:
        """Fetch a single account by its primary key.

        Args:
            account_id: Primary key of the account.

        Returns:
            AccountModel instance, or None if not found.
        """
        try:
            with self.db.session() as session:
                account = session.get(AccountModel, account_id)
                if account is None:
                    logger.debug('Account id=%d not found', account_id)
                return account
        except Exception as exc:
            logger.error(
                'Failed to fetch account id=%d: %s', account_id, exc,
            )
            raise

    # ───────────────────────────────────────────
    # Credential Helpers
    # ───────────────────────────────────────────

    def validate_api_credentials(
        self,
        api_key: str,
        api_secret: str,
        use_testnet: bool = False,
    ) -> bool:
        """Test whether API credentials can connect to the exchange.

        Creates a temporary ``ExchangeClient``, initialises it, and
        attempts a balance fetch.  Returns True on success, False on
        any authentication/connection error.

        Args:
            api_key: Plaintext API key.
            api_secret: Plaintext API secret.
            use_testnet: Whether to validate against the testnet.

        Returns:
            True if the credentials are valid and the exchange is reachable.
        """
        try:
            from copy import deepcopy

            test_settings = deepcopy(self.settings)
            test_settings.use_testnet = use_testnet

            client = ExchangeClient.for_account(
                test_settings, api_key, api_secret,
            )
            client.initialize()
            balance = client.fetch_balance()

            logger.info(
                'API credential validation succeeded (balance=%.4f, testnet=%s)',
                balance.get('total', 0.0), use_testnet,
            )
            return True

        except Exception as exc:
            logger.warning(
                'API credential validation failed: %s', exc,
            )
            return False

    def decrypt_account_keys(
        self, account: AccountModel
    ) -> tuple[str, str]:
        """Decrypt the encrypted API key and secret for an account.

        Args:
            account: AccountModel instance with encrypted credentials.

        Returns:
            Tuple of ``(plaintext_api_key, plaintext_api_secret)``.

        Raises:
            accounts.encryption.EncryptionError: If decryption fails
                (e.g. wrong master key, corrupted ciphertext).
        """
        api_key = self.encryption.decrypt(account.encrypted_api_key)
        api_secret = self.encryption.decrypt(account.encrypted_api_secret)
        logger.debug(
            'Decrypted keys for account id=%d label=%r',
            account.id, account.label,
        )
        return api_key, api_secret
