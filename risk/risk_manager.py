"""
Zentry Futures Core — Risk Manager.

The most critical module. Enforces:
  • 5% daily loss limit → close all, disable until next day
  • 25% max exposure → block new entries
  • 15% max drawdown → emergency shutdown, require manual restart
  • Emergency shutdown persistence via database
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from config.settings import Settings
from core.database import Database

logger = logging.getLogger(__name__)


class RiskManager:
    """Central risk management with daily limits, drawdown protection,
    exposure caps, and emergency shutdown capability.
    """

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self._high_water_mark: float = 0.0
        self._daily_start_balance: float = 0.0
        self._current_date: str = ''

    def initialize(self, balance: float) -> None:
        """Initialise risk state from database or fresh values.

        Must be called once on bot startup before any trading.

        Args:
            balance: Current account balance.
        """
        # High water mark
        hwm_str = self.database.get_state('high_water_mark')
        if hwm_str:
            self._high_water_mark = float(hwm_str)
        else:
            self._high_water_mark = balance
            self.database.set_state('high_water_mark', str(balance))

        if balance > self._high_water_mark:
            self._high_water_mark = balance
            self.database.set_state('high_water_mark', str(balance))

        # Daily start balance
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        saved_date = self.database.get_state('daily_start_date')
        saved_balance = self.database.get_state('daily_start_balance')

        if saved_date == today and saved_balance:
            self._daily_start_balance = float(saved_balance)
            self._current_date = today
        else:
            self._daily_start_balance = balance
            self._current_date = today
            self.database.set_state('daily_start_balance', str(balance))
            self.database.set_state('daily_start_date', today)

        logger.info(
            'Risk manager initialised: HWM=%.2f daily_start=%.2f date=%s',
            self._high_water_mark, self._daily_start_balance, self._current_date,
        )

    def can_open_position(
        self,
        margin: float,
        current_balance: float,
        current_exposure: float,
        active_positions: int,
    ) -> Tuple[bool, str]:
        """Pre-trade validation — checks all risk limits.

        Args:
            margin: Margin for the proposed new position.
            current_balance: Current account balance.
            current_exposure: Sum of all active position margins.
            active_positions: Count of active baskets.

        Returns:
            Tuple of (allowed: bool, reason: str).
        """
        # 1. Emergency shutdown (only CRITICAL shutdowns block here; routine
        #    daily drawdown never sets this flag).
        if self.is_emergency_shutdown():
            return False, 'Emergency shutdown active — critical failure, manual review required'

        # 2. Daily drawdown limit (account-size aware; pauses until next UTC day)
        if self.check_daily_loss_limit(current_balance):
            limit = self.settings.get_daily_drawdown_limit(current_balance)
            dd = self.get_daily_drawdown_pct(current_balance)
            return False, f'Daily drawdown limit reached ({dd:.1%} >= {limit:.0%})'

        # 3. Catastrophic drawdown from high-water mark (critical backstop)
        if self.check_drawdown_limit(current_balance):
            return False, (
                f'Catastrophic drawdown limit reached '
                f'({self.settings.catastrophic_drawdown_pct:.0%} from peak)'
            )

        # 4. Max positions
        max_pos = self.settings.get_max_positions(current_balance)
        if active_positions >= max_pos:
            return False, f'Max positions reached ({active_positions}/{max_pos})'

        # 5. Per-basket hard margin cap (account-size aware)
        hard_cap = self.settings.get_margin_hard_cap(current_balance)
        if margin > hard_cap:
            return False, (
                f'Margin {margin:.2f} exceeds per-trade hard cap {hard_cap:.2f} '
                f'({self.settings.margin_hard_cap_pct:.0%} of {current_balance:.2f})'
            )

        # 6. Exposure limit
        if current_balance > 0:
            new_exposure_pct = (current_exposure + margin) / current_balance
            if new_exposure_pct > self.settings.max_exposure_pct:
                return (
                    False,
                    f'Max exposure would be exceeded '
                    f'({new_exposure_pct:.1%} > {self.settings.max_exposure_pct:.0%})',
                )

        # 7. Sufficient balance
        if margin > current_balance * 0.5:
            return False, 'Margin too large relative to balance'

        if current_balance < 5.0:
            return False, 'Balance too low to trade safely'

        return True, 'OK'

    def get_daily_drawdown_pct(self, current_balance: float) -> float:
        """Current daily drawdown as a positive fraction (0.0 if in profit)."""
        if self._daily_start_balance <= 0:
            return 0.0
        change = (current_balance - self._daily_start_balance) / self._daily_start_balance
        return max(0.0, -change)

    def check_daily_loss_limit(self, current_balance: float) -> bool:
        """Check if the account-size-aware daily drawdown limit has been hit.

        Limit is 15% (<= $50), 10% ($50–200), or 5% (> $200). Routine and
        auto-resets at midnight UTC — does NOT cause a permanent shutdown.

        Args:
            current_balance: Current account balance.

        Returns:
            True if the daily drawdown limit is breached.
        """
        self._check_daily_reset(current_balance)

        if self._daily_start_balance <= 0:
            return False

        limit = self.settings.get_daily_drawdown_limit(current_balance)
        daily_pnl_pct = (
            (current_balance - self._daily_start_balance) / self._daily_start_balance
        )
        breached = daily_pnl_pct <= -limit
        if breached:
            logger.warning(
                'Daily drawdown limit breached: drawdown=%.2f%% limit=%.0f%% '
                'start=%.2f current=%.2f',
                self.get_daily_drawdown_pct(current_balance) * 100, limit * 100,
                self._daily_start_balance, current_balance,
            )
        return breached

    def check_drawdown_limit(self, current_balance: float) -> bool:
        """Check if CATASTROPHIC drawdown from the high-water mark is breached.

        This is the critical backstop (default 50%): a drop this large from the
        all-time peak signals a genuine failure, not routine market noise, and is
        the ONLY drawdown condition that triggers a permanent emergency shutdown.

        Args:
            current_balance: Current account balance.

        Returns:
            True if the catastrophic drawdown limit is breached.
        """
        if self._high_water_mark <= 0:
            return False

        drawdown = (self._high_water_mark - current_balance) / self._high_water_mark
        return drawdown >= self.settings.catastrophic_drawdown_pct

    def update_high_water_mark(self, balance: float) -> None:
        """Update HWM if balance is a new peak.

        Args:
            balance: Current account balance.
        """
        if balance > self._high_water_mark:
            self._high_water_mark = balance
            self.database.set_state('high_water_mark', str(balance))
            logger.info('New high water mark: %.2f USDT', balance)

    def record_daily_start(self, balance: float) -> None:
        """Record the daily starting balance if the date has changed.

        Args:
            balance: Current account balance.
        """
        self._check_daily_reset(balance)

    def get_daily_starting_balance(self) -> float:
        """Return today's starting balance."""
        return self._daily_start_balance

    # ───────────────────────────────────────────
    # Emergency Shutdown
    # ───────────────────────────────────────────

    def is_emergency_shutdown(self) -> bool:
        """Check if emergency shutdown flag is active."""
        return self.database.get_state('emergency_shutdown') == 'true'

    def is_critical_shutdown(self) -> bool:
        """True only if the active shutdown was flagged CRITICAL.

        Critical shutdowns (genuine system/logic failures or catastrophic
        drawdown) require manual review. Non-critical / legacy shutdowns are
        eligible for automatic recovery.
        """
        if not self.is_emergency_shutdown():
            return False
        return self.database.get_state('emergency_shutdown_critical') == 'true'

    def trigger_emergency_shutdown(self, reason: str, critical: bool = False) -> None:
        """Activate emergency shutdown.

        Args:
            reason: Human-readable reason for the shutdown.
            critical: If True, requires manual review (permanent until cleared).
                If False, it is a routine pause eligible for daily auto-recovery.
        """
        self.database.set_state('emergency_shutdown', 'true')
        self.database.set_state('emergency_shutdown_reason', reason)
        self.database.set_state('emergency_shutdown_critical', 'true' if critical else 'false')
        self.database.set_state('emergency_shutdown_time', str(time.time()))
        if critical:
            logger.critical(
                '🚨 CRITICAL EMERGENCY SHUTDOWN: %s — Trading disabled. '
                'Manual review required (run with --clear-shutdown to resume).',
                reason,
            )
        else:
            logger.warning(
                '⏸️ Trading paused (non-critical): %s — '
                'will auto-recover on the next UTC day.', reason,
            )

    def clear_emergency_shutdown(self) -> None:
        """Clear the emergency shutdown flag."""
        self.database.set_state('emergency_shutdown', 'false')
        self.database.set_state('emergency_shutdown_reason', '')
        self.database.set_state('emergency_shutdown_critical', 'false')
        logger.info('Emergency shutdown cleared')

    def auto_recover_if_eligible(self) -> bool:
        """Auto-clear a NON-critical emergency shutdown so trading can resume.

        Critical shutdowns are left untouched (manual review only). Used on
        startup and at the start of each new UTC day so routine daily-drawdown
        pauses never require manual intervention.

        Returns:
            True if a shutdown was cleared, False otherwise.
        """
        if not self.is_emergency_shutdown():
            return False
        if self.is_critical_shutdown():
            return False
        reason = self.database.get_state('emergency_shutdown_reason') or 'unknown'
        logger.info(
            'Auto-recovering from non-critical shutdown (reason: %s) — '
            'resuming trading.', reason,
        )
        self.clear_emergency_shutdown()
        return True

    # ───────────────────────────────────────────
    # Internal Helpers
    # ───────────────────────────────────────────

    def _check_daily_reset(self, current_balance: float) -> None:
        """Reset daily tracking if the UTC date has changed.

        Args:
            current_balance: Current account balance.
        """
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if today != self._current_date:
            # Save yesterday's stats
            if self._current_date:
                self.database.save_daily_stats({
                    'date': self._current_date,
                    'starting_balance': self._daily_start_balance,
                    'ending_balance': current_balance,
                    'realized_pnl': current_balance - self._daily_start_balance,
                })

            self._current_date = today
            self._daily_start_balance = current_balance
            self.database.set_state('daily_start_balance', str(current_balance))
            self.database.set_state('daily_start_date', today)
            logger.info(
                'Daily reset: new start balance=%.2f for %s',
                current_balance, today,
            )
