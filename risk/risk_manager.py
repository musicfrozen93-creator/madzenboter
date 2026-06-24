"""
ZenGrid — Risk Manager (per-account, USDT daily limits).

Enforces the two account-level safety limits and provides the risk-tracking
framework (daily-start balance, high-water mark, daily stats, emergency
shutdown). All state is per-account and persisted through the account-isolated
DB wrapper, so each account's limits are fully independent.

  • Daily Profit Target (per-tier): when daily PnL reaches it, STOP opening new
    trades for the rest of the UTC day. Existing positions keep being managed.
  • Daily Loss Limit (per-tier): when total daily PnL (realised + open
    unrealised) falls to it, CLOSE ALL open positions and STOP trading for the
    rest of the day.

Both limits auto-reset at the start of each UTC day. Priority order of the whole
system is: survival → drawdown control → consistency → profit.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Tuple

from config.settings import Settings

logger = logging.getLogger(__name__)


class RiskManager:
    """Per-account daily profit/loss limits and risk-tracking framework."""

    def __init__(self, settings: Settings, database) -> None:
        self.settings = settings
        self.database = database  # account-isolated DB wrapper
        self._high_water_mark: float = 0.0
        self._daily_start_balance: float = 0.0
        self._current_date: str = ''

    # ───────────────────────────────────────────
    # Lifecycle
    # ───────────────────────────────────────────

    def initialize(self, balance: float) -> None:
        """Initialise/refresh daily tracking and the high-water mark."""
        hwm_str = self.database.get_state('high_water_mark')
        self._high_water_mark = float(hwm_str) if hwm_str else balance
        if balance > self._high_water_mark:
            self._high_water_mark = balance
        self.database.set_state('high_water_mark', str(self._high_water_mark))

        today = self._utc_date()
        saved_date = self.database.get_state('daily_start_date')
        saved_balance = self.database.get_state('daily_start_balance')

        if saved_date == today and saved_balance:
            self._daily_start_balance = float(saved_balance)
            self._current_date = today
        else:
            self._begin_new_day(balance, today)

    def _begin_new_day(self, balance: float, today: str) -> None:
        """Record a fresh UTC day and clear the daily limit locks."""
        self._daily_start_balance = balance
        self._current_date = today
        self.database.set_state('daily_start_balance', str(balance))
        self.database.set_state('daily_start_date', today)
        self.database.set_state('daily_limit_date', today)
        self.database.set_state('daily_profit_locked', 'false')
        self.database.set_state('daily_loss_locked', 'false')
        # The per-account portfolio trailing profit lock also resets each UTC day.
        self.database.set_state('portfolio_profit_locked', 'false')
        self.database.set_state('peak_portfolio_profit', '')
        logger.info('Daily reset: start balance=%.2f for %s', balance, today)

    def _check_daily_reset(self, current_balance: float) -> None:
        """Roll over daily tracking when the UTC date changes."""
        today = self._utc_date()
        if today == self._current_date:
            return
        # Persist yesterday's stats (risk-tracking framework).
        if self._current_date:
            try:
                self.database.save_daily_stats({
                    'date': self._current_date,
                    'starting_balance': self._daily_start_balance,
                    'ending_balance': current_balance,
                    'realized_pnl': current_balance - self._daily_start_balance,
                })
            except Exception as e:
                logger.debug('Failed to save daily stats: %s', e)
        self._begin_new_day(current_balance, today)

    # ───────────────────────────────────────────
    # Daily realised PnL
    # ───────────────────────────────────────────

    def daily_realized_pnl(self) -> float:
        """Sum of today's closed-basket PnL for this account (USDT).

        Derived ONLY from trade history (closed baskets) — never from wallet
        balance, so deposits/withdrawals/transfers can never move it.
        """
        try:
            trades = self.database.get_today_trades()
            return float(sum(t.pnl for t in trades))
        except Exception as e:
            logger.debug('daily_realized_pnl failed: %s', e)
            return 0.0

    def daily_trading_pnl(self, open_unrealized: float) -> float:
        """Total daily TRADING PnL = realised (closed) + unrealised (open).

        This is the ONLY basis for the daily profit/loss limits. It excludes all
        wallet-balance changes (deposits, withdrawals, funding/spot transfers,
        internal Binance transfers, manual adjustments) by construction.
        """
        return self.daily_realized_pnl() + open_unrealized

    # ───────────────────────────────────────────
    # New-entry gate (profit target + loss lock)
    # ───────────────────────────────────────────

    def can_take_new_entry(self) -> Tuple[bool, str]:
        """Gate for opening a NEW basket (this account only; never global).

        Checked in the documented order: account lock status (PROTECTION lock,
        then emergency) → daily profit limit → daily loss limit. Existing baskets
        are unaffected. All lock state is per-account and persisted, so it
        survives restarts. PROTECTION lock is PERMANENT (admin reset only); the
        daily locks clear on the next UTC reset.
        """
        if self.is_protection_locked():
            return False, 'PROTECTION_LOCKED (account death protection — manual admin reset required)'
        if self.is_emergency_shutdown():
            return False, 'emergency shutdown active'
        if self._locked('daily_profit_locked'):
            return False, 'daily profit target reached — no new trades until next UTC day'
        if self._locked('daily_loss_locked'):
            return False, 'daily loss limit reached — no new trades until next UTC day'
        return True, 'OK'

    def update_profit_target(self, open_unrealized: float, tier: dict) -> bool:
        """Latch the profit lock once daily TRADING PnL reaches the tier target.

        Uses realised + unrealised PnL (never wallet balance). Returns True if
        NEW entries are now locked for the day.
        """
        if self._locked('daily_profit_locked'):
            return True
        total = self.daily_trading_pnl(open_unrealized)
        target = tier['daily_profit_target']
        if total >= target:
            self.database.set_state('daily_profit_locked', 'true')
            logger.info(
                'DAILY_PROFIT_TARGET | %s | trading_pnl=%.4f >= target=%.2f — no new '
                'trades for the rest of the UTC day (existing baskets still managed).',
                tier['id'], total, target,
            )
            return True
        return False

    def check_loss_limit(self, open_unrealized: float, tier: dict) -> bool:
        """Check the daily loss limit against realised + open unrealised PnL.

        Returns True when the tier limit is breached (caller must close ALL
        positions immediately — do not wait for losses to be realised). Once
        breached the lock is latched for the rest of the UTC day.
        """
        if self._locked('daily_loss_locked'):
            return True
        total = self.daily_trading_pnl(open_unrealized)
        limit = tier['daily_loss_limit']
        if total <= -limit:
            self.database.set_state('daily_loss_locked', 'true')
            logger.warning(
                'DAILY_LOSS_LIMIT | %s | trading_pnl=%.4f <= -%.2f (realised + open) '
                '— closing ALL baskets and locking the account until next UTC day.',
                tier['id'], total, limit,
            )
            return True
        return False

    def is_daily_loss_locked(self) -> bool:
        return self._locked('daily_loss_locked')

    def is_daily_profit_locked(self) -> bool:
        return self._locked('daily_profit_locked')

    # ───────────────────────────────────────────
    # Portfolio trailing profit lock (per-account)
    # ───────────────────────────────────────────

    def is_portfolio_profit_locked(self) -> bool:
        """True if the portfolio trailing profit lock is currently armed."""
        return self._locked('portfolio_profit_locked')

    def update_portfolio_profit_lock(self, open_unrealized: float, tier: dict) -> bool:
        """Arm / trail / fire the per-account portfolio trailing profit lock.

        Uses TOTAL open unrealised PnL across the account's positions (never
        wallet balance), so deposits/withdrawals can never move it. Behaviour:

          • Not armed → ARM when ``open_unrealized >= portfolio_lock_trigger``,
            storing ``peak_portfolio_profit``. Arming never closes (returns False).
          • Armed → trail the stored peak, then return True when
            ``open_unrealized <= portfolio_lock_floor`` (the caller must close
            ALL positions with reason 'portfolio_profit_lock').

        Per-account and DB-persisted (account-isolated). Independent of, and
        compatible with, the daily profit lock. Cleared by
        ``reset_portfolio_profit_lock`` (after all positions close) and by the
        UTC-day reset.
        """
        trigger = float(tier.get('portfolio_lock_trigger', 0.0))
        floor_lvl = float(tier.get('portfolio_lock_floor', 0.0))
        if trigger <= 0 or floor_lvl <= 0:
            return False

        if not self.is_portfolio_profit_locked():
            if open_unrealized >= trigger:
                self.database.set_state('portfolio_profit_locked', 'true')
                self.database.set_state('peak_portfolio_profit', str(open_unrealized))
                logger.info(
                    'PORTFOLIO_PROFIT_LOCK_ARMED | %s | unrealized=%.4f >= trigger=%.2f '
                    '(give-back floor=%.2f)',
                    tier['id'], open_unrealized, trigger, floor_lvl,
                )
            return False

        # Armed: trail the peak upward.
        try:
            peak = float(self.database.get_state('peak_portfolio_profit') or 0.0)
        except (TypeError, ValueError):
            peak = 0.0
        if open_unrealized > peak:
            peak = open_unrealized
            self.database.set_state('peak_portfolio_profit', str(peak))

        if open_unrealized <= floor_lvl:
            logger.warning(
                'PORTFOLIO_PROFIT_LOCK | %s | unrealized=%.4f <= floor=%.2f (peak=%.4f) '
                '— closing ALL positions.',
                tier['id'], open_unrealized, floor_lvl, peak,
            )
            return True
        return False

    def reset_portfolio_profit_lock(self) -> None:
        """Clear the portfolio profit lock + peak (after all positions close).

        Idempotent and write-light: only touches the DB when a lock is actually
        set, so calling it every management cycle with no open positions is cheap.
        """
        if self.database.get_state('portfolio_profit_locked') in (None, '', 'false'):
            return
        self.database.set_state('portfolio_profit_locked', 'false')
        self.database.set_state('peak_portfolio_profit', '')
        logger.info('PORTFOLIO_PROFIT_LOCK reset (positions flat / new day)')

    # ───────────────────────────────────────────
    # Account death protection (PERMANENT, admin reset only)
    # ───────────────────────────────────────────

    def is_protection_locked(self) -> bool:
        """True if the account is PROTECTION_LOCKED (permanent until admin reset)."""
        return self._locked('protection_locked')

    def check_account_death_protection(self, equity: float, tier: dict) -> bool:
        """Latch the PROTECTION lock if account equity falls below the tier floor.

        Equity = wallet balance + open floating PnL (the account's real value).
        Tier 1 floor $15, Tier 2 floor $30. Once tripped the lock is PERMANENT
        (it is NOT cleared by the UTC-day reset) and only an admin can remove it.
        Returns True if the account is protection-locked (now or already).

        Args:
            equity: Account equity (wallet balance + unrealised PnL).
            tier: The account's current tier config.
        """
        if self.is_protection_locked():
            return True
        floor = tier.get('protection_floor', 0.0)
        if floor > 0 and equity < floor:
            self.database.set_state('protection_locked', 'true')
            self.database.set_state('protection_locked_reason',
                                    f'equity {equity:.2f} < {tier["id"]} floor {floor:.2f}')
            self.database.set_state('protection_locked_time', str(time.time()))
            logger.critical(
                'PROTECTION_LOCKED | %s | equity=%.2f < floor=%.2f — closing ALL '
                'baskets and DISABLING trading permanently (manual admin reset required).',
                tier['id'], equity, floor,
            )
            return True
        return False

    def clear_protection_lock(self) -> None:
        """Remove the PROTECTION lock — ADMIN ONLY (manual maintenance action)."""
        self.database.set_state('protection_locked', 'false')
        self.database.set_state('protection_locked_reason', '')
        logger.warning('PROTECTION_LOCK cleared by admin')

    # ───────────────────────────────────────────
    # Emergency shutdown (risk-tracking framework)
    # ───────────────────────────────────────────

    def is_emergency_shutdown(self) -> bool:
        return self.database.get_state('emergency_shutdown') == 'true'

    def trigger_emergency_shutdown(self, reason: str) -> None:
        self.database.set_state('emergency_shutdown', 'true')
        self.database.set_state('emergency_shutdown_reason', reason)
        self.database.set_state('emergency_shutdown_time', str(time.time()))
        logger.critical('EMERGENCY SHUTDOWN: %s', reason)

    def clear_emergency_shutdown(self) -> None:
        self.database.set_state('emergency_shutdown', 'false')
        self.database.set_state('emergency_shutdown_reason', '')
        logger.info('Emergency shutdown cleared')

    def update_high_water_mark(self, balance: float) -> None:
        if balance > self._high_water_mark:
            self._high_water_mark = balance
            self.database.set_state('high_water_mark', str(balance))

    def get_daily_starting_balance(self) -> float:
        return self._daily_start_balance

    # ───────────────────────────────────────────
    # Internal helpers
    # ───────────────────────────────────────────

    def _locked(self, key: str) -> bool:
        return self.database.get_state(key) == 'true'

    @staticmethod
    def _utc_date() -> str:
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')
