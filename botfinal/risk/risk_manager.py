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
        # 1. Emergency shutdown
        if self.is_emergency_shutdown():
            return False, 'Emergency shutdown active — manual restart required'

        # 2. Daily loss limit
        if self.check_daily_loss_limit(current_balance):
            return False, 'Daily loss limit (5%) reached'

        # 3. Drawdown limit
        if self.check_drawdown_limit(current_balance):
            return False, 'Max drawdown limit (15%) reached'

        # 4. Max positions
        max_pos = self.settings.get_max_positions(current_balance)
        if active_positions >= max_pos:
            return False, f'Max positions reached ({active_positions}/{max_pos})'

        # 5. Exposure limit
        if current_balance > 0:
            new_exposure_pct = (current_exposure + margin) / current_balance
            if new_exposure_pct > self.settings.max_exposure_pct:
                return (
                    False,
                    f'Max exposure would be exceeded '
                    f'({new_exposure_pct:.1%} > {self.settings.max_exposure_pct:.0%})',
                )

        # 6. Sufficient balance
        if margin > current_balance * 0.5:
            return False, 'Margin too large relative to balance'

        if current_balance < 5.0:
            return False, 'Balance too low to trade safely'

        return True, 'OK'

    def check_daily_loss_limit(self, current_balance: float) -> bool:
        """Check if daily loss limit has been hit.

        Resets at midnight UTC.

        Args:
            current_balance: Current account balance.

        Returns:
            True if the 5% daily loss limit is breached.
        """
        self._check_daily_reset(current_balance)

        if self._daily_start_balance <= 0:
            return False

        daily_pnl_pct = (
            (current_balance - self._daily_start_balance) / self._daily_start_balance
        )
        return daily_pnl_pct <= -self.settings.daily_loss_limit_pct

    def check_drawdown_limit(self, current_balance: float) -> bool:
        """Check if max drawdown from high-water mark is breached.

        Args:
            current_balance: Current account balance.

        Returns:
            True if the 15% drawdown limit is breached.
        """
        if self._high_water_mark <= 0:
            return False

        drawdown = (self._high_water_mark - current_balance) / self._high_water_mark
        return drawdown >= self.settings.max_drawdown_pct

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

    def trigger_emergency_shutdown(self, reason: str) -> None:
        """Activate emergency shutdown — requires manual restart.

        Args:
            reason: Human-readable reason for the shutdown.
        """
        self.database.set_state('emergency_shutdown', 'true')
        self.database.set_state('emergency_shutdown_reason', reason)
        self.database.set_state('emergency_shutdown_time', str(time.time()))
        logger.critical(
            '🚨 EMERGENCY SHUTDOWN TRIGGERED: %s — '
            'Trading disabled. Run with --clear-shutdown to resume.',
            reason,
        )

    def clear_emergency_shutdown(self) -> None:
        """Clear the emergency shutdown flag."""
        self.database.set_state('emergency_shutdown', 'false')
        self.database.set_state('emergency_shutdown_reason', '')
        logger.info('Emergency shutdown cleared')

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
