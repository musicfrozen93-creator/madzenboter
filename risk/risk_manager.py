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
            # New UTC day → reset the daily profit trailing lock for this account.
            self._reset_daily_profit_lock()

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

        # 5. Per-basket hard margin cap (balance-tier based)
        hard_cap = self.settings.get_margin_hard_cap(current_balance)
        if margin > hard_cap:
            return False, (
                f'Margin {margin:.2f} exceeds per-basket tier cap {hard_cap:.2f} '
                f'(balance={current_balance:.2f})'
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
    # New-Entry Gate (profit lock + loss streak)
    # ───────────────────────────────────────────

    def can_take_new_entry(self, current_balance: float) -> Tuple[bool, str]:
        """Gate for OPENING a NEW basket (not recovery, not management).

        Blocks new entries when the daily profit lock is active or a loss-streak
        pause is in effect. Existing positions are unaffected and continue to be
        managed and closed normally.

        Args:
            current_balance: Current account balance.

        Returns:
            Tuple of (allowed: bool, reason: str).
        """
        if self.check_daily_profit_lock(current_balance):
            reason = self.database.get_state('profit_lock_reason') or 'floor'
            if reason == 'hard_stop':
                return False, (
                    f'daily profit hard-stop reached '
                    f'({self.settings.daily_profit_hard_stop_pct:.0%} gain) — '
                    f'no new entries for the rest of the day'
                )
            floor = float(self.database.get_state('profit_lock_floor') or 0.0)
            return False, (
                f'daily profit lock active (gain fell back to {floor:.0%} floor) '
                f'— no new entries for the rest of the day'
            )
        if self.is_loss_streak_paused():
            mins = self.loss_streak_pause_remaining() / 60.0
            return False, (
                f'loss-streak pause active ({mins:.0f}m remaining after '
                f'{self.settings.loss_streak_threshold} consecutive losing baskets)'
            )
        return True, 'OK'

    # ───────────────────────────────────────────
    # Daily Profit Trailing Lock (per-account, persisted)
    # ───────────────────────────────────────────

    def daily_gain_pct(self, current_balance: float) -> float:
        """Daily gain as a SIGNED fraction of the day's starting balance."""
        if self._daily_start_balance <= 0:
            return 0.0
        return (current_balance - self._daily_start_balance) / self._daily_start_balance

    def update_daily_profit_lock(self, current_balance: float) -> bool:
        """Arm/ratchet the daily profit floor and apply the hard stop.

        Ratchets the armed floor UP as daily gain crosses each tier (8%→5%,
        10%→8%, 12%→10%), and sets a sticky lock if either (a) gain reaches the
        hard-stop level (15%) or (b) gain falls back to the armed floor. The lock
        blocks NEW entries for the rest of the UTC day; existing positions are
        unaffected. State is persisted per-account and reset on the daily reset.

        Args:
            current_balance: Current account balance.

        Returns:
            True if NEW entries are currently locked for the day.
        """
        self._check_daily_reset(current_balance)
        if self._daily_start_balance <= 0:
            return False

        # Already locked for today → stays locked (sticky until the daily reset).
        if self.database.get_state('profit_lock_triggered') == 'true':
            return True

        gain = self.daily_gain_pct(current_balance)

        # Hard stop: stop new entries immediately for the rest of the day.
        if gain >= self.settings.daily_profit_hard_stop_pct:
            self.database.set_state('profit_lock_triggered', 'true')
            self.database.set_state('profit_lock_reason', 'hard_stop')
            logger.warning(
                'DAILY PROFIT HARD STOP: gain=%.2f%% >= %.2f%% — no new entries '
                'for the rest of the day (existing positions still managed).',
                gain * 100, self.settings.daily_profit_hard_stop_pct * 100,
            )
            return True

        # Ratchet the armed floor UP (never down) as gain crosses each tier.
        prev_floor = float(self.database.get_state('profit_lock_floor') or 0.0)
        floor = prev_floor
        for tier in self.settings.daily_profit_lock_tiers:
            if gain >= tier['gain'] and tier['floor'] > floor:
                floor = tier['floor']
        if floor > prev_floor:
            self.database.set_state('profit_lock_floor', str(floor))
            logger.info(
                'DAILY PROFIT FLOOR ARMED: gain=%.2f%% → floor=%.2f%% '
                '(locks new entries if gain falls back to the floor).',
                gain * 100, floor * 100,
            )

        # If a floor is armed and gain has fallen back to it → lock.
        if floor > 0.0 and gain <= floor:
            self.database.set_state('profit_lock_triggered', 'true')
            self.database.set_state('profit_lock_reason', 'floor')
            logger.warning(
                'DAILY PROFIT LOCK: gain=%.2f%% fell to floor=%.2f%% — locking in '
                'profit, no new entries for the rest of the day.',
                gain * 100, floor * 100,
            )
            return True

        return False

    def check_daily_profit_lock(self, current_balance: float) -> bool:
        """True if the daily profit lock blocks NEW entries (also updates state)."""
        return self.update_daily_profit_lock(current_balance)

    def _reset_daily_profit_lock(self) -> None:
        """Clear the per-account daily profit-lock state (new UTC day)."""
        self.database.set_state('profit_lock_floor', '0')
        self.database.set_state('profit_lock_triggered', 'false')
        self.database.set_state('profit_lock_reason', '')

    # ───────────────────────────────────────────
    # Loss-Streak Pause (per-account, persisted)
    # ───────────────────────────────────────────

    def record_basket_result(self, pnl: float) -> None:
        """Record a closed-basket outcome for loss-streak tracking.

        A losing basket (pnl < 0) increments the consecutive-loss counter; any
        winning/break-even basket resets it to zero. On reaching the configured
        threshold, a timed new-entry pause is armed (persisted, auto-expiring).

        Args:
            pnl: Realized PnL of the closed basket (USDT).
        """
        if pnl < 0:
            count = self._loss_streak_count() + 1
            self.database.set_state('loss_streak_count', str(count))
            if count >= self.settings.loss_streak_threshold:
                until = time.time() + self.settings.loss_streak_pause_seconds
                self.database.set_state('loss_streak_pause_until', str(until))
                # Reset the counter so a fresh streak is required after the pause.
                self.database.set_state('loss_streak_count', '0')
                logger.warning(
                    'LOSS-STREAK PAUSE: %d consecutive losing baskets — pausing '
                    'new entries for %d min (existing positions still managed).',
                    count, self.settings.loss_streak_pause_seconds // 60,
                )
        elif self._loss_streak_count() != 0:
            self.database.set_state('loss_streak_count', '0')

    def _loss_streak_count(self) -> int:
        """Current consecutive-loss count (0 if unset/invalid)."""
        try:
            return int(float(self.database.get_state('loss_streak_count') or 0))
        except (TypeError, ValueError):
            return 0

    def loss_streak_pause_remaining(self) -> float:
        """Seconds remaining on the loss-streak pause (0 if not paused)."""
        raw = self.database.get_state('loss_streak_pause_until')
        if not raw:
            return 0.0
        try:
            until = float(raw)
        except (TypeError, ValueError):
            return 0.0
        remaining = until - time.time()
        return remaining if remaining > 0 else 0.0

    def is_loss_streak_paused(self) -> bool:
        """True if a loss-streak pause is currently active (auto-expires)."""
        if self.loss_streak_pause_remaining() > 0:
            return True
        # Clear an expired marker so it doesn't linger in the state store.
        if self.database.get_state('loss_streak_pause_until') not in (None, '', '0'):
            self.database.set_state('loss_streak_pause_until', '0')
        return False

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
            # New UTC day → reset the daily profit trailing lock for this account.
            self._reset_daily_profit_lock()
            logger.info(
                'Daily reset: new start balance=%.2f for %s',
                current_balance, today,
            )
