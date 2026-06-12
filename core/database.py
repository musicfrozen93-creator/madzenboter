"""
ZenGrid — PostgreSQL Database Repository (SQLAlchemy).

Replaces the original SQLite database with PostgreSQL via SQLAlchemy ORM.
Preserves the exact same public API so all existing callers (TradingEngine,
RiskManager, CoinScanner, PositionManager) continue to work unchanged.

New methods are added for multi-account operations.
"""

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, List, Optional

from sqlalchemy import create_engine, or_, text
from sqlalchemy.orm import Session, sessionmaker

from core.dto import Basket, CoinScore, RecoveryLayer, TradeRecord
from core.models import (
    AccountModel,
    Base,
    BasketModel,
    BotStateModel,
    DailyStatModel,
    ExecutionLogModel,
    PositionModel,
    RecoveryLayerModel,
    RiskMetricModel,
    SignalModel,
    SubscriptionModel,
    TradeModel,
    UserModel,
    WatchlistModel,
)

logger = logging.getLogger(__name__)


class Database:
    """PostgreSQL repository for all bot persistence needs.

    Maintains backward-compatible API with the original SQLite Database class.
    All existing callers continue to work without modification.
    """

    def __init__(self, db_url: Optional[str] = None) -> None:
        """Initialise database connection.

        Args:
            db_url: PostgreSQL connection URL. Falls back to DATABASE_URL
                    env var, then to a local default.
        """
        self.db_url = (
            db_url
            or os.environ.get('DATABASE_URL')
            or 'postgresql://trading_bot:trading_bot@localhost:5432/trading_bot'
        )

        self.engine = create_engine(
            self.db_url,
            pool_size=20,
            max_overflow=30,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=False,
        )
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    # ───────────────────────────────────────────
    # Session Management
    # ───────────────────────────────────────────

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """Provide a transactional session scope.

        Yields:
            SQLAlchemy Session that auto-commits on success, rolls back on error.
        """
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_session(self) -> Session:
        """Get a new session (caller is responsible for commit/close).

        Returns:
            New SQLAlchemy Session.
        """
        return self.SessionLocal()

    # ───────────────────────────────────────────
    # Schema Initialisation
    # ───────────────────────────────────────────

    def initialize(self) -> None:
        """Create all tables if they do not exist."""
        Base.metadata.create_all(bind=self.engine)
        logger.info('Database initialised at %s', self.db_url.split('@')[-1])

    # ───────────────────────────────────────────
    # Basket Operations (backward-compatible)
    # ───────────────────────────────────────────

    def save_basket(self, basket: Basket) -> None:
        """Insert a new basket and all its layers.

        Args:
            basket: The Basket DTO to persist.
        """
        with self.session() as session:
            basket_orm = BasketModel(
                id=basket.id,
                account_id=getattr(basket, 'account_id', None),
                symbol=basket.symbol,
                side=basket.side,
                atr_at_entry=basket.atr_at_entry,
                volatility=basket.volatility,
                leverage=basket.leverage,
                status=basket.status,
                created_at=basket.created_at,
                template=getattr(basket, 'template', 'core') or 'core',
                risk_budget=getattr(basket, 'risk_budget', 0.0) or 0.0,
                wind_down=bool(getattr(basket, 'wind_down', False)),
                wind_down_at=getattr(basket, 'wind_down_at', None),
                peak_roi=getattr(basket, 'peak_roi', 0.0) or 0.0,
                be_armed=bool(getattr(basket, 'be_armed', False)),
            )
            session.merge(basket_orm)
            for layer in basket.layers:
                layer_orm = RecoveryLayerModel(
                    basket_id=basket.id,
                    layer_number=layer.layer_number,
                    entry_price=layer.entry_price,
                    margin=layer.margin,
                    quantity=layer.quantity,
                    side=layer.side,
                    timestamp=layer.timestamp,
                    status=layer.status,
                )
                session.add(layer_orm)

    def update_basket(self, basket: Basket) -> None:
        """Update an existing basket and upsert its layers.

        Args:
            basket: The Basket DTO with updated state.
        """
        with self.session() as session:
            basket_orm = session.get(BasketModel, basket.id)
            if basket_orm:
                basket_orm.status = basket.status
                basket_orm.leverage = basket.leverage
                basket_orm.template = getattr(basket, 'template', 'core') or 'core'
                basket_orm.risk_budget = getattr(basket, 'risk_budget', 0.0) or 0.0
                basket_orm.wind_down = bool(getattr(basket, 'wind_down', False))
                basket_orm.wind_down_at = getattr(basket, 'wind_down_at', None)
                basket_orm.peak_roi = getattr(basket, 'peak_roi', 0.0) or 0.0
                basket_orm.be_armed = bool(getattr(basket, 'be_armed', False))

            # Delete existing layers and re-insert
            session.query(RecoveryLayerModel).filter(
                RecoveryLayerModel.basket_id == basket.id
            ).delete()
            for layer in basket.layers:
                layer_orm = RecoveryLayerModel(
                    basket_id=basket.id,
                    layer_number=layer.layer_number,
                    entry_price=layer.entry_price,
                    margin=layer.margin,
                    quantity=layer.quantity,
                    side=layer.side,
                    timestamp=layer.timestamp,
                    status=layer.status,
                )
                session.add(layer_orm)

    def load_active_baskets(self, account_id: Optional[int] = None) -> List[Basket]:
        """Load all baskets with status == 'active', including their layers.

        Args:
            account_id: If provided, filter by account. None loads all.

        Returns:
            List of active Basket DTO instances.
        """
        with self.session() as session:
            query = session.query(BasketModel).filter(BasketModel.status == 'active')
            if account_id is not None:
                query = query.filter(BasketModel.account_id == account_id)

            rows = query.all()
            baskets: List[Basket] = []
            for row in rows:
                basket = Basket(
                    symbol=row.symbol,
                    side=row.side,
                    atr_at_entry=row.atr_at_entry,
                    volatility=row.volatility,
                    id=row.id,
                    created_at=row.created_at,
                    status=row.status,
                    leverage=row.leverage,
                    account_id=row.account_id,
                    template=getattr(row, 'template', 'core') or 'core',
                    risk_budget=getattr(row, 'risk_budget', 0.0) or 0.0,
                    wind_down=bool(getattr(row, 'wind_down', False)),
                    wind_down_at=getattr(row, 'wind_down_at', None),
                    peak_roi=getattr(row, 'peak_roi', 0.0) or 0.0,
                    be_armed=bool(getattr(row, 'be_armed', False)),
                )
                # Load layers
                layers = (
                    session.query(RecoveryLayerModel)
                    .filter(RecoveryLayerModel.basket_id == row.id)
                    .order_by(RecoveryLayerModel.layer_number)
                    .all()
                )
                for lr in layers:
                    layer = RecoveryLayer(
                        layer_number=lr.layer_number,
                        entry_price=lr.entry_price,
                        margin=lr.margin,
                        quantity=lr.quantity,
                        side=lr.side,
                        timestamp=lr.timestamp,
                        status=lr.status,
                    )
                    basket.layers.append(layer)
                baskets.append(basket)
            return baskets

    def close_basket(self, basket_id: str) -> None:
        """Mark a basket and all its layers as closed.

        Args:
            basket_id: The basket UUID to close.
        """
        with self.session() as session:
            session.query(BasketModel).filter(BasketModel.id == basket_id).update(
                {'status': 'closed'}
            )
            session.query(RecoveryLayerModel).filter(
                RecoveryLayerModel.basket_id == basket_id
            ).update({'status': 'closed'})

    # ───────────────────────────────────────────
    # Trade Operations (backward-compatible)
    # ───────────────────────────────────────────

    def save_trade(self, trade: TradeRecord) -> None:
        """Insert an immutable trade record.

        Args:
            trade: The TradeRecord DTO to persist.
        """
        with self.session() as session:
            trade_orm = TradeModel(
                id=trade.id,
                account_id=getattr(trade, 'account_id', None),
                basket_id=trade.basket_id,
                symbol=trade.symbol,
                side=trade.side,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                quantity=trade.quantity,
                margin=trade.margin,
                leverage=trade.leverage,
                pnl=trade.pnl,
                fee=trade.fee,
                layers_used=trade.layers_used,
                entry_time=trade.entry_time,
                exit_time=trade.exit_time,
                exit_reason=trade.exit_reason,
            )
            session.merge(trade_orm)

    def get_trades_since(
        self, timestamp: float, account_id: Optional[int] = None
    ) -> List[TradeRecord]:
        """Fetch all trades with exit_time >= timestamp.

        Args:
            timestamp: Unix timestamp to filter from.
            account_id: If provided, filter by account.

        Returns:
            List of TradeRecord DTO instances.
        """
        with self.session() as session:
            query = (
                session.query(TradeModel)
                .filter(TradeModel.exit_time >= timestamp)
                .order_by(TradeModel.exit_time)
            )
            if account_id is not None:
                query = query.filter(TradeModel.account_id == account_id)

            return [self._trade_orm_to_dto(r) for r in query.all()]

    def get_today_trades(self, account_id: Optional[int] = None) -> List[TradeRecord]:
        """Fetch all trades from the current UTC day.

        Args:
            account_id: If provided, filter by account.

        Returns:
            List of today's TradeRecord DTO instances.
        """
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        return self.get_trades_since(today_start, account_id=account_id)

    # ───────────────────────────────────────────
    # Watchlist Operations (backward-compatible)
    # ───────────────────────────────────────────

    def save_watchlist(self, scores: List[CoinScore]) -> None:
        """Replace the entire watchlist with new scores.

        Args:
            scores: List of CoinScore DTO entries.
        """
        with self.session() as session:
            session.query(WatchlistModel).delete()
            now = time.time()
            for s in scores:
                wl = WatchlistModel(
                    symbol=s.symbol,
                    volume_24h=s.volume_24h,
                    atr=s.atr,
                    atr_score=s.atr_score,
                    volume_score=s.volume_score,
                    spread_score=s.spread_score,
                    funding_rate=s.funding_rate,
                    funding_score=s.funding_score,
                    composite_score=s.composite_score,
                    updated_at=now,
                    tier=getattr(s, 'tier', 'core') or 'core',
                )
                session.add(wl)

    def get_watchlist(self) -> List[CoinScore]:
        """Load the current watchlist ordered by composite score.

        Returns:
            List of CoinScore DTO entries, highest score first.
        """
        with self.session() as session:
            rows = (
                session.query(WatchlistModel)
                .order_by(WatchlistModel.composite_score.desc())
                .all()
            )
            return [
                CoinScore(
                    symbol=r.symbol,
                    volume_24h=r.volume_24h or 0.0,
                    atr=r.atr or 0.0,
                    atr_score=r.atr_score or 0.0,
                    volume_score=r.volume_score or 0.0,
                    spread_score=r.spread_score or 0.0,
                    funding_rate=r.funding_rate or 0.0,
                    funding_score=r.funding_score or 0.0,
                    composite_score=r.composite_score or 0.0,
                    tier=getattr(r, 'tier', 'core') or 'core',
                )
                for r in rows
            ]

    # ───────────────────────────────────────────
    # Bot State (backward-compatible KV store)
    # ───────────────────────────────────────────

    def set_state(self, key: str, value: str) -> None:
        """Upsert a key-value pair into bot_state.

        Args:
            key: State key.
            value: State value (always stored as string).
        """
        with self.session() as session:
            state = session.get(BotStateModel, key)
            if state:
                state.value = value
                state.updated_at = time.time()
            else:
                session.add(BotStateModel(
                    key=key, value=value, updated_at=time.time()
                ))

    def get_state(self, key: str) -> Optional[str]:
        """Retrieve a value from bot_state.

        Args:
            key: State key to look up.

        Returns:
            The value string, or None if key does not exist.
        """
        with self.session() as session:
            state = session.get(BotStateModel, key)
            return state.value if state else None

    # ───────────────────────────────────────────
    # Daily Statistics (backward-compatible, now per-account)
    # ───────────────────────────────────────────

    def save_daily_stats(self, stats: dict, account_id: Optional[int] = None) -> None:
        """Insert or replace daily statistics.

        Args:
            stats: Dict with keys: date, starting_balance, ending_balance,
                   realized_pnl, total_trades, winning_trades, losing_trades,
                   max_drawdown.
            account_id: If provided, associate stats with an account.
        """
        with self.session() as session:
            date_str = stats.get('date', '')
            # Check if exists
            existing = (
                session.query(DailyStatModel)
                .filter(
                    DailyStatModel.account_id == account_id,
                    DailyStatModel.date == date_str,
                )
                .first()
            )
            if existing:
                existing.starting_balance = stats.get('starting_balance', 0)
                existing.ending_balance = stats.get('ending_balance', 0)
                existing.realized_pnl = stats.get('realized_pnl', 0)
                existing.total_trades = stats.get('total_trades', 0)
                existing.winning_trades = stats.get('winning_trades', 0)
                existing.losing_trades = stats.get('losing_trades', 0)
                existing.max_drawdown = stats.get('max_drawdown', 0)
                existing.created_at = time.time()
            else:
                session.add(DailyStatModel(
                    account_id=account_id,
                    date=date_str,
                    starting_balance=stats.get('starting_balance', 0),
                    ending_balance=stats.get('ending_balance', 0),
                    realized_pnl=stats.get('realized_pnl', 0),
                    total_trades=stats.get('total_trades', 0),
                    winning_trades=stats.get('winning_trades', 0),
                    losing_trades=stats.get('losing_trades', 0),
                    max_drawdown=stats.get('max_drawdown', 0),
                    created_at=time.time(),
                ))

    # ───────────────────────────────────────────
    # Signal Operations (NEW)
    # ───────────────────────────────────────────

    def save_signal(self, signal) -> int:
        """Persist a signal for audit trail.

        Args:
            signal: Signal DTO from signal_engine.

        Returns:
            The auto-generated signal ID.
        """
        with self.session() as session:
            sig = SignalModel(
                symbol=signal.symbol,
                side=signal.side,
                strength=signal.strength,
                atr=signal.atr,
                market_regime=signal.market_regime,
                volatility=signal.volatility,
                current_price=signal.current_price,
                ema200=signal.ema200,
                rsi=signal.rsi,
                symbol_state=getattr(signal, 'symbol_state', None),
                btc_state=getattr(signal, 'btc_state', None),
                relative_strength=getattr(signal, 'relative_strength', None),
                alignment_score=getattr(signal, 'alignment_score', None),
            )
            session.add(sig)
            session.flush()
            return sig.id

    # ───────────────────────────────────────────
    # Account Operations (NEW)
    # ───────────────────────────────────────────

    def get_active_accounts(self) -> List[AccountModel]:
        """Fetch all active trading accounts (account flag only).

        Returns:
            List of AccountModel ORM instances where is_active is True.
        """
        with self.session() as session:
            return (
                session.query(AccountModel)
                .filter(AccountModel.is_active.is_(True))
                .order_by(AccountModel.id)
                .all()
            )

    def get_tradeable_accounts(self) -> List[AccountModel]:
        """Fetch accounts eligible to OPEN new trades.

        The database is the single source of truth. An account is tradeable
        only when ALL hold:
          • the account is enabled (accounts.is_active)
          • the owning user exists and is active (users.is_active)
          • the user has an active, non-expired subscription
            (subscriptions.status = 'active' AND (expires_at IS NULL OR > now))

        Returns:
            List of eligible AccountModel ORM instances, ordered by id.
        """
        now = datetime.now(timezone.utc)
        with self.session() as session:
            active_sub_user_ids = (
                session.query(SubscriptionModel.user_id)
                .filter(SubscriptionModel.status == 'active')
                .filter(or_(
                    SubscriptionModel.expires_at.is_(None),
                    SubscriptionModel.expires_at > now,
                ))
            )
            return (
                session.query(AccountModel)
                .join(UserModel, AccountModel.user_id == UserModel.id)
                .filter(AccountModel.is_active.is_(True))
                .filter(UserModel.is_active.is_(True))
                .filter(AccountModel.user_id.in_(active_sub_user_ids))
                .order_by(AccountModel.id)
                .all()
            )

    def get_account_eligibility(self) -> List[tuple]:
        """Per-account trade eligibility with a human-readable reason.

        Evaluates every account that is enabled at the account level and
        reports why it is or isn't tradeable. Used for transparent skip
        logging during signal fan-out.

        Returns:
            List of (AccountModel, eligible: bool, reason: str).
        """
        now = datetime.now(timezone.utc)
        results: List[tuple] = []
        with self.session() as session:
            accounts = (
                session.query(AccountModel)
                .filter(AccountModel.is_active.is_(True))
                .order_by(AccountModel.id)
                .all()
            )
            for acct in accounts:
                user = session.get(UserModel, acct.user_id)
                if user is None:
                    results.append((acct, False, 'owning user not found'))
                    continue
                if not user.is_active:
                    results.append((acct, False, 'user suspended'))
                    continue
                sub = (
                    session.query(SubscriptionModel)
                    .filter(SubscriptionModel.user_id == acct.user_id)
                    .filter(SubscriptionModel.status == 'active')
                    .filter(or_(
                        SubscriptionModel.expires_at.is_(None),
                        SubscriptionModel.expires_at > now,
                    ))
                    .first()
                )
                if sub is None:
                    results.append((acct, False, 'no active subscription'))
                    continue
                results.append((acct, True, 'OK'))
        return results

    def get_account_by_id(self, account_id: int) -> Optional[AccountModel]:
        """Fetch a single account by ID.

        Args:
            account_id: Account primary key.

        Returns:
            AccountModel or None.
        """
        with self.session() as session:
            return session.get(AccountModel, account_id)

    def get_all_accounts(self) -> List[AccountModel]:
        """Fetch all trading accounts (active and inactive).

        Returns:
            List of AccountModel ORM instances.
        """
        with self.session() as session:
            return session.query(AccountModel).order_by(AccountModel.id).all()

    # ───────────────────────────────────────────
    # Execution Log Operations (NEW)
    # ───────────────────────────────────────────

    def save_execution_log(
        self,
        account_id: int,
        action: str,
        symbol: str,
        status: str,
        signal_id: Optional[int] = None,
        side: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Record an execution log entry.

        Args:
            account_id: Account that executed.
            action: Action type ('open', 'close', 'recovery', 'sync').
            symbol: Trading pair.
            status: Result status ('success', 'failed', 'skipped').
            signal_id: Associated signal ID if applicable.
            side: Trade side.
            quantity: Executed quantity.
            price: Execution price.
            error_message: Error details if failed.
        """
        with self.session() as session:
            session.add(ExecutionLogModel(
                account_id=account_id,
                signal_id=signal_id,
                action=action,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                status=status,
                error_message=error_message,
            ))

    # ───────────────────────────────────────────
    # Risk Metrics Operations (NEW)
    # ───────────────────────────────────────────

    def save_risk_metrics(
        self,
        account_id: int,
        daily_loss: float,
        max_drawdown: float,
        current_exposure: float,
        high_water_mark: float,
        open_positions_count: int,
        daily_start_balance: float,
        current_balance: float,
    ) -> None:
        """Insert a risk metrics snapshot for an account.

        Args:
            account_id: Account ID.
            daily_loss: Current day loss amount.
            max_drawdown: Drawdown from HWM.
            current_exposure: Total margin in use.
            high_water_mark: Peak balance.
            open_positions_count: Number of open positions.
            daily_start_balance: Balance at day start.
            current_balance: Current balance.
        """
        with self.session() as session:
            session.add(RiskMetricModel(
                account_id=account_id,
                daily_loss=daily_loss,
                max_drawdown=max_drawdown,
                current_exposure=current_exposure,
                high_water_mark=high_water_mark,
                open_positions_count=open_positions_count,
                daily_start_balance=daily_start_balance,
                current_balance=current_balance,
            ))

    # ───────────────────────────────────────────
    # Position Operations (NEW)
    # ───────────────────────────────────────────

    def get_positions(
        self,
        account_id: Optional[int] = None,
        status: Optional[str] = 'open',
    ) -> List[PositionModel]:
        """Fetch positions, optionally filtered by account and status.

        Args:
            account_id: If provided, filter by account.
            status: If provided, filter by status ('open', 'closed').

        Returns:
            List of PositionModel ORM instances.
        """
        with self.session() as session:
            query = session.query(PositionModel)
            if account_id is not None:
                query = query.filter(PositionModel.account_id == account_id)
            if status is not None:
                query = query.filter(PositionModel.status == status)
            return query.order_by(PositionModel.opened_at.desc()).all()

    def get_all_trades(
        self,
        account_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[TradeModel]:
        """Fetch trade records with pagination.

        Args:
            account_id: If provided, filter by account.
            limit: Max records to return.
            offset: Pagination offset.

        Returns:
            List of TradeModel ORM instances.
        """
        with self.session() as session:
            query = session.query(TradeModel)
            if account_id is not None:
                query = query.filter(TradeModel.account_id == account_id)
            return (
                query.order_by(TradeModel.exit_time.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )

    # ───────────────────────────────────────────
    # Internal Helpers
    # ───────────────────────────────────────────

    def _trade_orm_to_dto(self, row: TradeModel) -> TradeRecord:
        """Convert a TradeModel ORM instance to a TradeRecord DTO.

        Args:
            row: TradeModel from the database.

        Returns:
            TradeRecord DTO instance.
        """
        return TradeRecord(
            id=row.id,
            basket_id=row.basket_id,
            symbol=row.symbol,
            side=row.side,
            entry_price=row.entry_price,
            exit_price=row.exit_price,
            quantity=row.quantity,
            margin=row.margin,
            leverage=row.leverage,
            pnl=row.pnl,
            fee=row.fee,
            layers_used=row.layers_used,
            entry_time=row.entry_time,
            exit_time=row.exit_time,
            exit_reason=row.exit_reason,
            account_id=row.account_id,
        )

    def close(self) -> None:
        """Dispose of the engine connection pool."""
        if self.engine:
            self.engine.dispose()
            logger.info('Database connection pool disposed')
