"""V4 Orchestrator — the decision pipeline (spec §14).

Ties every V4 module into the single flow from the blueprint:

  regime → engine router → signal → stop → circuit/heat → sizing → safety → open
  (and, each tick) reconcile → circuit → manage/flatten

Components are dependency-injected so the orchestrator is unit-testable with fakes
and so production wiring (live exchange client, DB-backed stores) is a matter of
construction, not code change. The orchestrator never touches the network itself;
callers pass in the already-fetched market data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from v4.account_state import AccountRiskState
from v4.engines.base import V4Signal
from v4.execution.position_manager import V4Position, V4PositionManager
from v4.execution.trade_manager import TickReport, V4TradeManager
from v4.kill_switch import KillSwitch
from v4.params import V4Params
from v4.portfolio import (
    CircuitDecision,
    check_directional_heat,
    evaluate_circuit_breakers,
)
from v4.regime import RegimeEngine
from v4.safety import SafetyResult, validate_entry
from v4.sizing import OpenExposure, PositionSizerV4
from v4.trade_math import compute_stop


@dataclass
class MarketData:
    """Everything the orchestrator needs for one symbol on one evaluation."""

    symbol: str
    df: object                 # OHLCV DataFrame (trading timeframe)
    market_info: dict
    candle_age_seconds: float
    df_btc: Optional[object] = None


@dataclass
class AccountContext:
    """Live per-account state for one evaluation (no network inside)."""

    account_id: int
    wallet_balance: float
    open_unrealized: float
    realized_today: float
    realized_week: float
    realized_month: float
    open_positions: List[V4Position]


@dataclass
class OpenAttempt:
    opened: Optional[V4Position]
    approved: bool
    reason: str
    stage: str
    signal: Optional[V4Signal] = None


def _open_exposure(positions: List[V4Position]) -> tuple[OpenExposure, float, float]:
    """Aggregate open exposure + per-side risk from live positions."""
    risk = notional = margin = 0.0
    long_risk = short_risk = 0.0
    count = 0
    for p in positions:
        if p.closed:
            continue
        count += 1
        r = p.open_risk_usd
        n = p.state.entry * p.state.remaining_qty
        m = n / p.leverage if p.leverage else 0.0
        risk += r
        notional += n
        margin += m
        if p.side == 'long':
            long_risk += r
        else:
            short_risk += r
    return OpenExposure(risk, notional, margin, count), long_risk, short_risk


class V4Orchestrator:
    def __init__(
        self,
        position_manager: V4PositionManager,
        kill_switch: KillSwitch,
        state_store_factory,                 # (account_id) -> AccountStateStore
        params: Optional[V4Params] = None,
        regime_engine: Optional[RegimeEngine] = None,
        trend_engine=None,
        breakout_engine=None,
        sizer: Optional[PositionSizerV4] = None,
        logger=None,
    ) -> None:
        self.params = params or V4Params()
        self.pm = position_manager
        self.tm = V4TradeManager(position_manager, self.params)
        self.kill = kill_switch
        self._state_store_factory = state_store_factory
        self.regime_engine = regime_engine or RegimeEngine(self.params)
        # Engines are optional to allow injection; imported lazily to avoid a hard
        # dependency when a caller supplies its own.
        if trend_engine is None or breakout_engine is None:
            from v4.engines import BreakoutEngine, TrendEngine
            trend_engine = trend_engine or TrendEngine(self.params)
            breakout_engine = breakout_engine or BreakoutEngine(self.params)
        self.trend_engine = trend_engine
        self.breakout_engine = breakout_engine
        self.sizer = sizer or PositionSizerV4(self.params)
        self.logger = logger

    def _risk_state(self, account_id: int) -> AccountRiskState:
        return AccountRiskState(self._state_store_factory(account_id), self.params)

    def circuit_for(self, ctx: AccountContext, now: Optional[datetime] = None) -> CircuitDecision:
        now = now or datetime.now(timezone.utc)
        rs = self._risk_state(ctx.account_id)
        ci = rs.circuit_inputs(
            now, ctx.wallet_balance, ctx.open_unrealized,
            ctx.realized_today, ctx.realized_week, ctx.realized_month,
        )
        return evaluate_circuit_breakers(ci, self.params)

    # ── OPEN pipeline ──
    def evaluate_and_open(
        self, md: MarketData, ctx: AccountContext, now: Optional[datetime] = None,
    ) -> OpenAttempt:
        now = now or datetime.now(timezone.utc)
        equity = ctx.wallet_balance + ctx.open_unrealized

        # Kill switch (global/account) — hard gate on new entries.
        if not self.kill.can_open(ctx.account_id):
            return OpenAttempt(None, False, f'kill_switch:{self.kill.effective_level(ctx.account_id)}', 'kill')

        circuit = self.circuit_for(ctx, now)

        regime = self.regime_engine.classify(md.df, md.df_btc)
        signal = None
        if regime.armed_engine == 'trend':
            signal = self.trend_engine.generate(md.symbol, md.df, regime)
        elif regime.armed_engine == 'breakout':
            signal = self.breakout_engine.generate(md.symbol, md.df, regime)

        stop = None
        plan = None
        heat = None
        if signal is not None:
            stop = compute_stop(
                signal.entry, signal.side, signal.atr,
                structural_level=signal.structural_level, params=self.params,
            )
            open_exp, long_risk, short_risk = _open_exposure(ctx.open_positions)
            lim = self._risk_state(ctx.account_id).limits(equity)
            if stop.valid:
                plan = self.sizer.build_plan(
                    equity=equity, side=signal.side, entry=signal.entry,
                    stop_price=stop.stop_price, market_info=md.market_info,
                    symbol=md.symbol, open_exposure=open_exp, limits=lim,
                    risk_multiplier=circuit.risk_multiplier,
                )
                if plan.suitable:
                    heat = check_directional_heat(
                        signal.side, plan.risk_usd, long_risk, short_risk, equity, self.params,
                    )

        result: SafetyResult = validate_entry(
            regime=regime, signal=signal, stop=stop, plan=plan, heat=heat,
            circuit=circuit, candle_age_seconds=md.candle_age_seconds,
            is_duplicate=self._is_duplicate(md.symbol, signal, ctx),
            params=self.params,
        )
        if not result.approved:
            return OpenAttempt(None, False, result.reason, result.stage, signal)

        strong = regime.direction.value in ('trend_up', 'trend_down') and \
            regime.volatility.value == 'expansion'
        pos = self.pm.open(plan, account_id=ctx.account_id, strong_trend=strong)
        if pos is None:
            return OpenAttempt(None, False, 'open_failed_or_no_fill', 'execution', signal)
        return OpenAttempt(pos, True, 'OK', 'opened', signal)

    @staticmethod
    def _is_duplicate(symbol: str, signal: Optional[V4Signal], ctx: AccountContext) -> bool:
        """Never open a second position on a symbol/side already held (idempotency)."""
        if signal is None:
            return False
        for p in ctx.open_positions:
            if not p.closed and p.symbol == symbol and p.side == signal.side:
                return True
        return False

    # ── MANAGE pipeline ──
    def manage(
        self,
        ctx: AccountContext,
        price_map: Dict[str, float],
        atr_map: Dict[str, float],
        live_keys: Optional[set] = None,
        now: Optional[datetime] = None,
    ) -> TickReport:
        """Manage all of an account's open positions for one tick."""
        # Kill-switch flatten overrides circuit breakers.
        if self.kill.must_flatten(ctx.account_id):
            flat = CircuitDecision(False, True, True, False, 1.0, 'kill_switch_flatten', 'flatten')
            return self.tm.manage_tick(ctx.open_positions, price_map, atr_map, flat, live_keys)
        circuit = self.circuit_for(ctx, now)
        return self.tm.manage_tick(ctx.open_positions, price_map, atr_map, circuit, live_keys)


def build_orchestrator(
    exchange_client,
    raw_state_store,
    params: Optional[V4Params] = None,
    logger=None,
    sleep_func=None,
) -> V4Orchestrator:
    """Assemble a fully-wired V4Orchestrator for one account's exchange client.

    This is the production wiring seam: given a ccxt-like client (per account) and
    a raw get_state/set_state store (DB-backed in production), it constructs the
    order/stop/position managers, the kill switch, and the orchestrator with the
    real regime + engines. Kept as a factory so the live loop wires V4 by
    construction, not by code change.
    """
    import time as _time

    from v4.account_state import AccountStateStore
    from v4.execution import (
        BinanceOrderManager,
        ExchangeStopManager,
        V4PositionManager,
    )

    p = params or V4Params()
    om = BinanceOrderManager(exchange_client, p, sleep_func=sleep_func or _time.sleep, logger=logger)
    pm = V4PositionManager(om, ExchangeStopManager(om), p)
    ks = KillSwitch(raw_state_store)
    return V4Orchestrator(
        position_manager=pm,
        kill_switch=ks,
        state_store_factory=lambda aid: AccountStateStore(raw_state_store, aid),
        params=p,
        logger=logger,
    )
