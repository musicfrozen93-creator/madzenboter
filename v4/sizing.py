"""Position Sizing + Dynamic Leverage (spec §C1).

This is the mathematically-closed risk stack. Every quantity is derived from the
STOP, never from leverage; leverage is DERIVED to keep per-trade margin inside
budget; and three portfolio caps (risk-at-once, total notional, total margin) are
enforced before an order could ever be sent. The audit (§A1) proved these caps
close simultaneously given the 1% stop floor and a 10× leverage ceiling; the unit
tests assert that closure.

Core identity (spec §C1):
    Risk$    = Equity × Risk%
    Quantity = Risk$ / stop_distance_price          (floored to the lot step)
    Notional = Quantity × Entry
    Leverage = ceil(Notional / MarginBudgetPerTrade), capped at max_leverage
    Margin   = Notional / Leverage

No order is ever placed from this module — it only produces a validated plan or a
rejection with an exact, loggable reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from exchange.utils import round_quantity, validate_min_notional
from v4.params import AccountLimits, V4Params, resolve_account_limits


@dataclass(frozen=True)
class OpenExposure:
    """Aggregate of the account's already-open V4 positions (for cap checks)."""

    risk_usd: float = 0.0
    notional_usd: float = 0.0
    margin_usd: float = 0.0
    open_count: int = 0


@dataclass(frozen=True)
class SizingPlan:
    """Result of a sizing attempt — either suitable, or rejected with a reason."""

    suitable: bool
    reason: str
    side: str = ''
    symbol: str = ''
    entry: float = 0.0
    stop_price: float = 0.0
    stop_distance: float = 0.0
    stop_pct: float = 0.0
    risk_pct_used: float = 0.0
    risk_usd: float = 0.0
    quantity: float = 0.0
    notional: float = 0.0
    leverage: int = 0
    margin: float = 0.0


def derive_leverage(notional: float, margin_budget: float, max_leverage: int) -> int:
    """Smallest integer leverage that fits `notional` into `margin_budget`.

    Leverage is an OUTPUT, never an input to sizing (spec §C1, §A1). Returns 0 if
    the notional cannot fit even at max_leverage (caller must reject the trade).
    """
    if notional <= 0 or margin_budget <= 0:
        return 0
    needed = math.ceil(notional / margin_budget)
    if needed > max_leverage:
        return 0
    return max(1, needed)


class PositionSizerV4:
    """Risk-based sizing with derived leverage and full pre-order validation."""

    def __init__(self, params: Optional[V4Params] = None) -> None:
        self.params = params or V4Params()

    def build_plan(
        self,
        equity: float,
        side: str,
        entry: float,
        stop_price: float,
        market_info: dict,
        symbol: str = '',
        open_exposure: Optional[OpenExposure] = None,
        limits: Optional[AccountLimits] = None,
        risk_multiplier: float = 1.0,
    ) -> SizingPlan:
        """Build a fully-validated position plan or reject with an exact reason.

        Args:
            equity: Live account equity (USDT).
            side: 'long' or 'short'.
            entry: Intended entry price.
            stop_price: Structural stop price (from trade_math.compute_stop).
            market_info: CCXT market dict (precision / limits).
            open_exposure: Aggregate of already-open V4 positions for cap checks.
            limits: Pre-resolved AccountLimits (resolved here if omitted).
        """
        p = self.params
        open_exp = open_exposure or OpenExposure()
        lim = limits or resolve_account_limits(equity, p)

        if not lim.tradeable:
            return SizingPlan(False, lim.reason, side=side, symbol=symbol, entry=entry)

        side = (side or '').lower()
        if side not in ('long', 'short'):
            return SizingPlan(False, f'invalid_side ({side!r})', symbol=symbol, entry=entry)
        if not (entry > 0):
            return SizingPlan(False, 'invalid_entry_price', side=side, symbol=symbol)
        if not (stop_price > 0):
            return SizingPlan(False, 'invalid_stop_price', side=side, symbol=symbol, entry=entry)

        # Stop must be on the losing side of entry.
        if side == 'long' and stop_price >= entry:
            return SizingPlan(False, 'stop_not_below_entry_for_long', side=side, symbol=symbol, entry=entry)
        if side == 'short' and stop_price <= entry:
            return SizingPlan(False, 'stop_not_above_entry_for_short', side=side, symbol=symbol, entry=entry)

        # ── Concurrency cap ──
        if open_exp.open_count >= lim.max_concurrent:
            return SizingPlan(
                False,
                f'max_concurrent_reached ({open_exp.open_count}/{lim.max_concurrent})',
                side=side, symbol=symbol, entry=entry,
            )

        # ── Stop distance with floor/ceiling (spec §C2) ──
        raw_distance = abs(entry - stop_price)
        raw_pct = raw_distance / entry
        if raw_pct > p.stop_ceiling_pct + 1e-12:
            return SizingPlan(
                False,
                f'stop_too_wide ({raw_pct:.4f} > {p.stop_ceiling_pct:.4f}) — skip',
                side=side, symbol=symbol, entry=entry, stop_price=stop_price,
                stop_distance=raw_distance, stop_pct=raw_pct,
            )
        # Clamp the distance UP to the noise floor (never trade a sub-1% stop).
        stop_pct = max(raw_pct, p.stop_floor_pct)
        stop_distance = stop_pct * entry
        # Re-derive the effective stop price at the floored distance.
        eff_stop_price = entry - stop_distance if side == 'long' else entry + stop_distance

        # ── Risk → quantity, with the small-account min-notional bump (spec §C2) ──
        min_notional = (
            market_info.get('limits', {}).get('cost', {}).get('min')
            or p_min_notional_floor()
        )
        risk_mult = max(0.0, float(risk_multiplier))
        risk_pct = lim.risk_pct * risk_mult
        plan = self._try_size(
            risk_pct, lim.equity, entry, eff_stop_price, side, stop_distance,
            stop_pct, market_info, min_notional, lim, open_exp, symbol,
        )
        if plan.suitable or plan.reason != 'below_min_notional':
            return plan
        # Small-account bump: only below the ceiling, only to clear min-notional.
        if lim.equity < p.small_account_ceiling:
            bumped = self._try_size(
                p.small_account_max_risk_pct * risk_mult, lim.equity, entry, eff_stop_price,
                side, stop_distance, stop_pct, market_info, min_notional, lim,
                open_exp, symbol,
            )
            return bumped
        return plan

    def _try_size(
        self, risk_pct, equity, entry, stop_price, side, stop_distance, stop_pct,
        market_info, min_notional, lim: AccountLimits, open_exp: OpenExposure,
        symbol: str = '',
    ) -> SizingPlan:
        p = self.params
        risk_usd = equity * risk_pct
        raw_qty = risk_usd / stop_distance
        qty = round_quantity(raw_qty, market_info)

        base = SizingPlan(
            False, 'unknown', side=side, symbol=symbol, entry=entry, stop_price=stop_price,
            stop_distance=stop_distance, stop_pct=stop_pct, risk_pct_used=risk_pct,
            risk_usd=risk_usd,
        )

        if qty <= 0:
            return _reason(base, 'quantity_rounds_to_zero')

        min_qty = market_info.get('limits', {}).get('amount', {}).get('min') or 0.0
        if min_qty and qty < min_qty:
            return _reason(base, f'below_min_quantity ({qty:.8f} < {min_qty:.8f})')

        notional = qty * entry
        if not validate_min_notional(qty, entry, market_info):
            return _reason(base, 'below_min_notional')

        # ── Derive leverage to fit the per-trade margin budget (spec §C1) ──
        leverage = derive_leverage(
            notional, lim.margin_budget_per_trade_usd, p.max_leverage
        )
        if leverage <= 0:
            return _reason(
                base,
                f'exceeds_margin_budget (notional {notional:.2f} needs > {p.max_leverage}× '
                f'given budget {lim.margin_budget_per_trade_usd:.2f})',
            )
        margin = notional / leverage

        # ── Actual risk after lot rounding (qty was floored) ──
        actual_risk = qty * stop_distance

        # ── Portfolio caps (spec §C1) ──
        if actual_risk + open_exp.risk_usd > lim.max_risk_at_once_usd + 1e-9:
            return _reason(
                base,
                f'risk_at_once_cap ({actual_risk + open_exp.risk_usd:.4f} > '
                f'{lim.max_risk_at_once_usd:.4f})',
            )
        if notional + open_exp.notional_usd > lim.total_notional_cap_usd + 1e-9:
            return _reason(
                base,
                f'total_notional_cap ({notional + open_exp.notional_usd:.2f} > '
                f'{lim.total_notional_cap_usd:.2f})',
            )
        if margin + open_exp.margin_usd > lim.total_margin_cap_usd + 1e-9:
            return _reason(
                base,
                f'total_margin_cap ({margin + open_exp.margin_usd:.2f} > '
                f'{lim.total_margin_cap_usd:.2f})',
            )

        return SizingPlan(
            suitable=True, reason='OK', side=side, symbol=symbol, entry=entry,
            stop_price=stop_price, stop_distance=stop_distance, stop_pct=stop_pct,
            risk_pct_used=risk_pct, risk_usd=round(actual_risk, 6),
            quantity=qty, notional=round(notional, 6), leverage=leverage,
            margin=round(margin, 6),
        )


def _reason(plan: SizingPlan, reason: str) -> SizingPlan:
    """Return a copy of a rejected plan carrying a specific reason."""
    return SizingPlan(
        suitable=False, reason=reason, side=plan.side, symbol=plan.symbol,
        entry=plan.entry, stop_price=plan.stop_price, stop_distance=plan.stop_distance,
        stop_pct=plan.stop_pct, risk_pct_used=plan.risk_pct_used,
        risk_usd=plan.risk_usd,
    )


def p_min_notional_floor() -> float:
    """Fallback exchange min-notional when the market dict omits it (Binance $5)."""
    return 5.0
