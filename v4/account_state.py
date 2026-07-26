"""Per-account risk state + multi-account isolation + auto-scaling (spec §C1, §C5, §A2).

Two pieces:
  • AccountStateStore — namespaces every state key by account id so each account's
    peak equity, day-peak PnL, and loss streak are fully isolated (the V4 analogue
    of V1's AccountDatabaseWrapper).
  • AccountRiskState  — derives the CircuitInputs (daily/weekly/monthly PnL %,
    drawdown, consecutive losses, day-peak PnL) that drive the circuit breakers,
    and resolves the auto-scaled per-account limits from live equity.

PnL percentages use TRADING PnL only (realised closed-trade PnL + open unrealised)
over the period's starting balance — never wallet balance — so deposits and
withdrawals cannot move the limits (carried from V1's correct design).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from v4.params import V4Params, AccountLimits, resolve_account_limits
from v4.portfolio import CircuitInputs


class AccountStateStore:
    """Account-isolated view over a raw get_state/set_state store."""

    def __init__(self, raw_store, account_id: int) -> None:
        self._store = raw_store
        self.account_id = account_id

    def _key(self, key: str) -> str:
        return f'v4_account_{self.account_id}_{key}'

    def get_state(self, key: str) -> Optional[str]:
        return self._store.get_state(self._key(key))

    def set_state(self, key: str, value: str) -> None:
        self._store.set_state(self._key(key), value)


class AccountRiskState:
    """Derives circuit-breaker inputs and auto-scaled limits for one account."""

    def __init__(self, store: AccountStateStore, params: Optional[V4Params] = None) -> None:
        self.store = store
        self.params = params or V4Params()

    # ── auto-scaling (spec §C5) ──
    def limits(self, equity: float) -> AccountLimits:
        return resolve_account_limits(equity, self.params)

    # ── loss streak (spec §C2 cooldown) ──
    def record_trade_result(self, pnl: float) -> int:
        streak = self._int('consecutive_losses')
        streak = streak + 1 if pnl < 0 else 0
        self.store.set_state('consecutive_losses', str(streak))
        return streak

    # ── circuit inputs (spec §A2, §C2) ──
    def circuit_inputs(
        self,
        now: datetime,
        wallet_balance: float,
        open_unrealized: float,
        realized_today: float,
        realized_week: float,
        realized_month: float,
    ) -> CircuitInputs:
        """Compute the CircuitInputs snapshot for this account.

        `wallet_balance` is the realised cash balance (already includes today's
        realised PnL). `open_unrealized` is current floating PnL. Period start
        balances are derived on the fly as `wallet_balance − realised_period`, so
        no start-balance persistence is needed and deposits only make the % more
        conservative.
        """
        equity = wallet_balance + open_unrealized

        def pct(realized_period: float) -> float:
            start = wallet_balance - realized_period
            trading_pnl = realized_period + open_unrealized
            return (trading_pnl / start) if start > 0 else 0.0

        daily = pct(realized_today)
        weekly = pct(realized_week)
        monthly = pct(realized_month)

        # Peak equity + drawdown.
        peak = self._float('peak_equity', default=equity)
        if equity > peak:
            peak = equity
            self.store.set_state('peak_equity', str(peak))
        elif self.store.get_state('peak_equity') is None:
            self.store.set_state('peak_equity', str(peak))
        drawdown = (peak - equity) / peak if peak > 0 else 0.0

        # Day-peak daily-PnL% (resets each UTC day).
        today = now.strftime('%Y-%m-%d')
        stored_day = self.store.get_state('day_peak_date')
        if stored_day != today:
            day_peak = daily
            self.store.set_state('day_peak_date', today)
        else:
            day_peak = max(self._float('day_peak_pnl_pct', default=daily), daily)
        self.store.set_state('day_peak_pnl_pct', str(day_peak))

        return CircuitInputs(
            daily_pnl_pct=daily,
            weekly_pnl_pct=weekly,
            monthly_pnl_pct=monthly,
            drawdown_pct=max(0.0, drawdown),
            consecutive_losses=self._int('consecutive_losses'),
            day_peak_pnl_pct=day_peak,
        )

    # ── helpers ──
    def _int(self, key: str, default: int = 0) -> int:
        raw = self.store.get_state(key)
        try:
            return int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    def _float(self, key: str, default: float = 0.0) -> float:
        raw = self.store.get_state(key)
        try:
            return float(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default
