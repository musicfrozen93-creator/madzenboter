"""
Zentry Futures Core — Position Manager.

Orchestrates position lifecycle: opening, recovery layers,
take-profit, stop-loss, and closing. The central coordinator
between grid, risk, and exchange modules.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

from config.settings import Settings, VolatilityLevel
from core.database import Database
from core.dto import Basket, RecoveryLayer, Signal, TradeRecord
from exchange.client import ExchangeClient
from exchange.utils import round_quantity, validate_min_notional
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from grid.templates import TemplateRouter
from market.market_state import MarketState
from market.symbol_state import SymbolStateEngine
from portfolio.manager import PortfolioManager
from risk.account_profile import classify_account_profile, get_profile_policy
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from risk.stop_loss import StopLossManager
from signals.signal_engine import SignalEngine

logger = logging.getLogger(__name__)
trade_logger = logging.getLogger('trades')

# Live-ATR cache TTL — basket management refreshes a symbol's 5m ATR at
# most this often so spacing/exit maths breathe with current volatility
# without an OHLCV fetch on every 10s loop pass.
_ATR_CACHE_TTL_SECONDS = 120.0


class PositionManager:
    """Manages the full lifecycle of trading baskets.

    Responsibilities:
      • Open new positions on entry signals
      • Add recovery layers when price moves against
      • Monitor basket/individual TP and SL
      • Close positions (full basket or individual layers)
      • Coordinate with risk manager for all pre-trade checks
    """

    def __init__(
        self,
        exchange_client: ExchangeClient,
        settings: Settings,
        database: Database,
        risk_manager: RiskManager,
        position_sizer: PositionSizer,
        recovery_system: RecoverySystem,
        tp_manager: TakeProfitManager,
        sl_manager: StopLossManager,
        signal_engine: SignalEngine,
        template_router: Optional[TemplateRouter] = None,
        portfolio_manager: Optional[PortfolioManager] = None,
        symbol_state_engine: Optional[SymbolStateEngine] = None,
    ) -> None:
        self.exchange = exchange_client
        self.settings = settings
        self.database = database
        self.risk_manager = risk_manager
        self.position_sizer = position_sizer
        self.recovery = recovery_system
        self.tp_manager = tp_manager
        self.sl_manager = sl_manager
        self.signal_engine = signal_engine
        # V2 components — all optional; when absent the manager degrades to
        # V1-equivalent behaviour (CORE template, no portfolio budgets).
        self.template_router = template_router or TemplateRouter(settings)
        self.portfolio_manager = portfolio_manager
        self.symbol_state_engine = symbol_state_engine
        self._atr_cache: Dict[str, Tuple[float, float]] = {}

    # ───────────────────────────────────────────
    # Open Position
    # ───────────────────────────────────────────

    def open_position(
        self,
        signal: Signal,
        balance: float,
        market_state: Optional[MarketState] = None,
    ) -> Optional[Basket]:
        """Open a new position (Layer 1) based on a signal.

        V2 flow: route the signal into a trade template (CORE/SCOUT/RANGE),
        size the plan with the template multiplier, evaluate portfolio
        budgets (which may demote the template or block), run the risk
        gates, then execute. The basket carries its template and a
        pre-committed risk budget fixed at entry.

        Args:
            signal: Entry signal from signal engine.
            balance: Current account balance.
            market_state: Global market state (None = V1-equivalent routing).

        Returns:
            New Basket if successful, None if blocked or failed.
        """
        try:
            vol = VolatilityLevel(signal.volatility)
        except ValueError:
            vol = VolatilityLevel.MEDIUM

        leverage = self.settings.get_leverage(vol)

        # ── V2: route the signal into a trade template ──
        demoted_sides = set()
        try:
            demoted_sides = self.risk_manager.get_demoted_sides()
        except Exception as e:
            logger.debug('Demoted-sides lookup failed: %s', e)

        template, alignment, route_reason = self.template_router.route(
            signal, market_state, demoted_sides
        )
        signal.alignment_score = alignment
        policy = self.settings.get_template_policy(template.value)
        profile = classify_account_profile(balance, self.settings)

        logger.info(
            'POSITION_OPEN_REQUEST %s %s | balance=%.2f vol=%s lev=%dx price=%.4f '
            'template=%s profile=%s align=%.2f',
            signal.side.upper(), signal.symbol, balance, vol.value, leverage,
            signal.current_price, template.value, profile.value, alignment,
        )

        # Get market info for quantity calculation
        try:
            market_info = self.exchange.get_symbol_info(signal.symbol)
        except Exception as e:
            logger.error('Failed to get market info for %s: %s', signal.symbol, e)
            logger.info('SIGNAL_REJECTED %s | stage=market_info | %s', signal.symbol, e)
            return None

        # ── Account-size-aware order plan + symbol suitability ──
        # evaluate_entry accounts for the exchange min-notional / min-lot, so the
        # margin/notional below reflect the SMALLEST order that would actually
        # fill — and reject the symbol if that breaches the account hard cap or
        # sits too close to liquidation.
        plan = self.position_sizer.evaluate_entry(
            balance, signal.current_price, leverage, vol, market_info,
            size_multiplier=policy['size_multiplier'],
        )

        if not plan['suitable']:
            logger.info(
                'SIGNAL_REJECTED %s | stage=suitability | reason=%s | balance=%.2f '
                'price=%.4f lev=%dx req_margin=%.2f notional=%.2f liq_dist=%.1f%% hard_cap=%.2f',
                signal.symbol, plan['reason'], balance, signal.current_price,
                leverage, plan['margin'], plan['notional'],
                plan['liquidation_distance_pct'] * 100, plan['hard_cap'],
            )
            return None

        # Calculate current exposure from active baskets
        active_baskets = self.database.load_active_baskets()
        current_exposure = sum(b.total_margin for b in active_baskets)

        # Existing-basket protection: never open a second basket on the same symbol.
        if any(b.symbol == signal.symbol for b in active_baskets):
            logger.info(
                'SIGNAL_REJECTED %s | stage=existing_basket | an active basket already exists for this symbol',
                signal.symbol,
            )
            return None

        # ── V2: pre-committed basket risk budget (fixed at entry) ──
        # Recovery layers redistribute risk INSIDE this budget — they never
        # enlarge the maximum loss. Floored at 10% of the first-layer margin
        # so exchange-floor-inflated entries on small accounts still get a
        # meaningful budget.
        risk_budget = max(
            balance * policy['risk_budget_pct'], plan['margin'] * 0.10
        )

        # ── V2: portfolio budgets (may demote the template or block) ──
        if self.portfolio_manager is not None:
            recent_losses = PortfolioManager.recent_realized_losses(
                self.database, self.settings.event_window_hours
            )
            final_template, p_reason = self.portfolio_manager.evaluate(
                signal, template, plan['margin'], leverage, risk_budget,
                active_baskets, balance, market_state, recent_losses,
            )
            if final_template is None:
                logger.info(
                    'SIGNAL_REJECTED %s | stage=portfolio | reason=%s', signal.symbol, p_reason,
                )
                return None
            if final_template != template:
                # Demoted — re-plan with the smaller template's sizing.
                template = final_template
                policy = self.settings.get_template_policy(template.value)
                plan = self.position_sizer.evaluate_entry(
                    balance, signal.current_price, leverage, vol, market_info,
                    size_multiplier=policy['size_multiplier'],
                )
                if not plan['suitable']:
                    logger.info(
                        'SIGNAL_REJECTED %s | stage=portfolio_demotion | '
                        'demoted plan unsuitable: %s', signal.symbol, plan['reason'],
                    )
                    return None
                risk_budget = max(
                    balance * policy['risk_budget_pct'], plan['margin'] * 0.10
                )

        quantity = plan['quantity']
        margin = plan['margin']

        # Pre-trade risk check (margin + notional exposure)
        current_notional = sum(
            b.total_margin * max(b.leverage, 1) for b in active_baskets
        )
        allowed, reason = self.risk_manager.can_open_position(
            margin, balance, current_exposure, len(active_baskets),
            new_notional=margin * leverage, current_notional=current_notional,
        )
        if not allowed:
            logger.info(
                'SIGNAL_REJECTED %s | stage=risk | reason=%s | margin=%.2f exposure=%.2f positions=%d',
                signal.symbol, reason, margin, current_exposure, len(active_baskets),
            )
            return None

        logger.info(
            'SIGNAL_ACCEPTED %s %s | balance=%.2f lev=%dx | margin=%.2f '
            'notional=%.2f qty=%.8f liq_dist=%.1f%% hard_cap=%.2f | '
            'template=%s risk_budget=%.2f | %s',
            signal.side.upper(), signal.symbol, balance, leverage, margin,
            plan['notional'], quantity, plan['liquidation_distance_pct'] * 100,
            plan['hard_cap'], template.value, risk_budget, route_reason,
        )

        # ── Execute ──
        try:
            self.exchange.set_margin_mode(signal.symbol, 'cross')
            self.exchange.set_leverage(signal.symbol, leverage)

            order_side = 'buy' if signal.side == 'long' else 'sell'
            order = self.exchange.place_market_order(
                signal.symbol, order_side, quantity
            )

            fill_price = float(
                order.get('average', order.get('price', signal.current_price)) or
                signal.current_price
            )

            layer = RecoveryLayer(
                layer_number=1,
                entry_price=fill_price,
                margin=margin,
                quantity=quantity,
                side=signal.side,
            )

            basket = Basket(
                symbol=signal.symbol,
                side=signal.side,
                atr_at_entry=signal.atr,
                volatility=signal.volatility,
                leverage=leverage,
                template=template.value,
                risk_budget=round(risk_budget, 4),
            )
            basket.add_layer(layer)
            self.database.save_basket(basket)

            logger.info(
                'POSITION_OPEN_SUCCESS %s %s | basket=%s price=%.4f qty=%.8f '
                'margin=%.2f lev=%dx template=%s budget=%.2f',
                signal.side.upper(), signal.symbol, basket.id[:8],
                fill_price, quantity, margin, leverage, template.value, risk_budget,
            )
            trade_logger.info(
                'OPEN %s %s L1 | price=%.4f qty=%.8f margin=%.4f '
                'lev=%dx vol=%s regime=%s template=%s budget=%.4f | basket=%s',
                signal.side.upper(), signal.symbol, fill_price,
                quantity, margin, leverage, signal.volatility,
                signal.market_regime, template.value, risk_budget, basket.id[:8],
            )

            return basket

        except Exception as e:
            logger.error('Failed to open position for %s: %s', signal.symbol, e)
            logger.info('SIGNAL_REJECTED %s | stage=order_submission | %s', signal.symbol, e)
            return None

    # ───────────────────────────────────────────
    # Manage Baskets
    # ───────────────────────────────────────────

    def manage_baskets(
        self,
        baskets: List[Basket],
        balance: float,
        market_state: Optional[MarketState] = None,
    ) -> List[Basket]:
        """Main management loop for all active baskets (V2 lifecycle).

        Per-basket order of operations:
          1. Stops — pre-committed risk-budget stop (V2 baskets), then the
             emergency/basket-SL backstops. Per-layer SLs apply ONLY to
             legacy baskets (risk_budget == 0): for V2 baskets the budget
             IS the stop, which removes the V1 anchor-stop/L4-trigger
             collision.
          2. Wind-down handling — invalidated baskets exit at
             break-even-or-better or at the time budget; no new layers.
          3. Take profit — trailing beyond target (CORE) or fixed target
             scaled by the template multiplier.
          4. Break-even ratchet — a ≥2-layer basket that recovered to
             profit never round-trips back to its stop.
          5. Premise monitor — symbol trend state opposing the basket
             enters wind-down.
          6. Winner harvesting gate — partial closes / per-layer TPs only
             while the basket is net-positive.
          7. Time triage — stagnant near-flat baskets are recycled.
          8. Recovery — premise/factor-gated, template+profile-conditional
             layer counts and spacing, live-ATR ratcheted distances.

        Args:
            baskets: List of active baskets.
            balance: Current account balance.
            market_state: Global market state (None degrades V2 gates).

        Returns:
            Updated list of still-active baskets.
        """
        remaining: List[Basket] = []
        profile = classify_account_profile(balance, self.settings)
        profile_policy = get_profile_policy(profile, self.settings)

        for basket in baskets:
            if basket.status != 'active':
                continue
            if basket.layer_count == 0:
                # Consistency repair: an 'active' basket with no active
                # layers holds no exchange position but still counts against
                # max_positions and blocks its symbol (possible after a crash
                # between the last layer-close write and the basket-close
                # write). Close it so the slot is released.
                logger.warning(
                    'Reconciling zero-layer active basket %s %s — releasing slot',
                    basket.symbol, basket.id[:8],
                )
                basket.status = 'closed'
                self.database.close_basket(basket.id)
                continue

            try:
                ticker = self.exchange.fetch_ticker(basket.symbol)
                current_price = ticker['last']

                if current_price <= 0:
                    remaining.append(basket)
                    continue

                policy = self.settings.get_template_policy(
                    getattr(basket, 'template', 'core') or 'core'
                )
                is_v2 = (getattr(basket, 'risk_budget', 0.0) or 0.0) > 0.0

                # Live-ATR ratchet (V2 baskets only): spacing/exit distances
                # may widen with expanding volatility, never tighten
                # mid-basket. Legacy baskets keep V1's frozen entry ATR so
                # pre-V2 customer positions behave exactly as before (C3).
                if is_v2:
                    atr = max(basket.atr_at_entry, self._get_live_atr(basket.symbol))
                else:
                    atr = basket.atr_at_entry
                total_margin = basket.total_margin
                unrealized = basket.unrealized_pnl(current_price)
                roi = unrealized / total_margin if total_margin > 0 else 0.0

                # ── PRIORITY 1: Stops ──
                closed = False
                if is_v2:
                    # H1: ONE deterministic stop computation. The binding
                    # basket stop is the TIGHTER of the pre-committed risk
                    # budget and the margin-based backstop (basket_sl_pct ×
                    # total margin); the exit reason names whichever bound.
                    # Previously these were independent elif checks with no
                    # explicit precedence — the budget was dead weight for
                    # shallow baskets and the documentation lied about which
                    # stop governed.
                    margin_stop = self.settings.basket_sl_pct * total_margin
                    stop_level = (
                        min(basket.risk_budget, margin_stop)
                        if basket.risk_budget > 0 else margin_stop
                    )
                    if unrealized <= -stop_level:
                        reason = (
                            'risk_budget_sl'
                            if 0 < basket.risk_budget <= margin_stop
                            else 'basket_sl'
                        )
                        logger.warning(
                            'BASKET STOP: %s %s | loss=%.4f stop=%.4f '
                            '(budget=%.4f margin_stop=%.4f -> %s)',
                            basket.side.upper(), basket.symbol, unrealized,
                            stop_level, basket.risk_budget, margin_stop, reason,
                        )
                        self.close_basket(basket, reason)
                        closed = True
                    elif self.sl_manager.check_emergency_sl(
                        basket, current_price, balance
                    ):
                        self.close_basket(basket, 'emergency_sl')
                        closed = True
                    elif basket.layer_count == 1 and self.sl_manager.check_individual_sl(
                        basket.active_layers[0], current_price, atr, basket.side
                    ):
                        # H1: the V2 design removed per-layer stops only
                        # INSIDE an active ladder (where they collided with
                        # recovery triggers). A single-layer basket has no
                        # ladder — it keeps the tight V1 3×ATR stop instead
                        # of waiting for the much wider margin/budget stop.
                        # The stop turns off the moment Layer 2 is added.
                        self.close_basket(basket, 'individual_sl')
                        closed = True
                else:
                    # Legacy baskets: exact V1 stop chain (C3) — emergency,
                    # then basket SL, then per-layer stops.
                    if self.sl_manager.check_emergency_sl(
                        basket, current_price, balance
                    ):
                        self.close_basket(basket, 'emergency_sl')
                        closed = True
                    elif self.sl_manager.check_basket_sl(basket, current_price):
                        self.close_basket(basket, 'basket_sl')
                        closed = True
                    else:
                        for layer in list(basket.active_layers):
                            if self.sl_manager.check_individual_sl(
                                layer, current_price, atr, basket.side
                            ):
                                self._close_single_layer(basket, layer, current_price)
                        if basket.layer_count == 0:
                            basket.status = 'closed'
                            self.database.close_basket(basket.id)
                            closed = True

                if closed:
                    continue

                # ── PRIORITY 2: Wind-down handling ──
                if basket.wind_down:
                    # H2 + participation-regression fix. The fee-aware floor
                    # (net-positive exit) is the PREFERRED target — but
                    # requiring it for the entire window caused wind-downs
                    # that hover near zero to stall the full
                    # wind_down_max_hours, pinning a position slot, the
                    # symbol, counter-factor notional, and event-budget risk
                    # (the post-update participation collapse). The floor
                    # therefore applies for the FIRST HALF of the window
                    # only; after that it falls back to the configured
                    # epsilon — the pre-update gross break-even threshold —
                    # so stalled wind-downs exit at the first break-even
                    # tick and release the account's capacity.
                    started = basket.wind_down_at or basket.created_at
                    elapsed = time.time() - started
                    window = self.settings.wind_down_max_hours * 3600.0
                    wind_floor = max(
                        self.settings.wind_down_be_epsilon_roi,
                        self._net_floor_roi(basket),
                    )
                    if elapsed > window * 0.5:
                        wind_floor = self.settings.wind_down_be_epsilon_roi
                    if roi >= wind_floor:
                        self.close_basket(basket, 'wind_down')
                        continue
                    if elapsed > window:
                        self.close_basket(basket, 'wind_down_timeout')
                        continue
                    # Winding down: no TP arming, no layers, no harvesting.
                    self.database.update_basket(basket)
                    remaining.append(basket)
                    continue

                # ── PRIORITY 3: Take Profits ──
                # Legacy baskets keep V1's IMMEDIATE basket TP (C3): trailing
                # applies only to V2 baskets whose template enables it.
                if is_v2 and policy['trailing_enabled']:
                    trail_reason = self.tp_manager.check_trailing_tp(
                        basket, current_price, policy['tp_roi_multiplier']
                    )
                    if trail_reason:
                        self.close_basket(basket, trail_reason)
                        continue
                elif self.tp_manager.check_basket_tp(
                    basket, current_price,
                    policy['tp_roi_multiplier'] if is_v2 else 1.0,
                ):
                    self.close_basket(basket, 'basket_tp')
                    continue

                # ── PRIORITY 4: Break-even ratchet (V2 baskets only) ──
                # A multi-layer basket that recovered to profit locks the
                # recovery: it never round-trips back into its stop.
                # H2: the floor is fee-aware — it must cover round-trip
                # taker fees + exit slippage (which scale with leverage,
                # since fees are charged on notional while ROI is on
                # margin), so the locked outcome is net-non-negative. The
                # arm threshold always sits above the effective floor.
                if is_v2 and basket.layer_count >= 2:
                    be_floor = max(
                        self.settings.be_ratchet_floor_roi,
                        self._net_floor_roi(basket),
                    )
                    be_arm = max(self.settings.be_ratchet_arm_roi, be_floor + 0.005)
                    if not basket.be_armed and roi >= be_arm:
                        basket.be_armed = True
                        logger.info(
                            'BE RATCHET ARMED: %s %s | roi=%.2f%% layers=%d '
                            '(floor=%.2f%%)',
                            basket.side.upper(), basket.symbol,
                            roi * 100, basket.layer_count, be_floor * 100,
                        )
                    elif basket.be_armed and roi <= be_floor:
                        self.close_basket(basket, 'break_even_exit')
                        continue

                # ── PRIORITY 5: Premise monitor (V2 baskets only) ──
                # Legacy baskets are never wound down and never trigger the
                # symbol-state fetch (C3: exact V1 behaviour, no extra load).
                premise_ok = self._premise_ok(basket) if is_v2 else True
                if not premise_ok:
                    self._enter_wind_down(basket)
                    self.database.update_basket(basket)
                    remaining.append(basket)
                    continue

                # ── PRIORITY 6: Winner harvesting ──
                # V2 baskets: only while net-positive (closing profitable
                # layers out of a LOSING basket raises its break-even — the
                # V1 leak). Legacy baskets keep V1's ungated harvesting (C3).
                if (not is_v2) or unrealized >= 0:
                    partial_layers = self.tp_manager.check_partial_close(
                        basket, current_price
                    )
                    if partial_layers and len(partial_layers) < basket.layer_count:
                        self._close_single_layer(
                            basket, partial_layers[0], current_price
                        )

                    for layer in list(basket.active_layers):
                        if self.tp_manager.check_individual_tp(
                            layer, current_price, atr, basket.side
                        ):
                            self._close_single_layer(basket, layer, current_price)

                    if basket.layer_count == 0:
                        basket.status = 'closed'
                        self.database.close_basket(basket.id)
                        continue

                # ── PRIORITY 7: Time triage ──
                # Near-flat baskets past the template's holding budget pin
                # margin and block the symbol; recycle the slot.
                max_hold = policy['max_hold_hours']
                if (
                    max_hold and is_v2
                    and time.time() - basket.created_at > max_hold * 3600.0
                    and abs(roi) < self.settings.time_triage_roi_band
                ):
                    self.close_basket(basket, 'time_triage')
                    continue

                # ── PRIORITY 8: Recovery Layers ──
                if is_v2:
                    factor_impulse = (
                        market_state.impulse_against(basket.side)
                        if market_state is not None else False
                    )
                    spacing = (
                        policy['spacing_multiplier']
                        * profile_policy['spacing_multiplier']
                    )
                    max_layers = min(
                        int(policy['max_layers']), int(profile_policy['max_layers'])
                    )
                    next_layer = self.recovery.check_recovery_trigger(
                        basket, current_price, atr,
                        max_layers=max_layers,
                        spacing_multiplier=spacing,
                        premise_ok=premise_ok,
                        factor_impulse_against=factor_impulse,
                    )
                    if next_layer is not None:
                        self._add_recovery_layer(
                            basket, next_layer, balance, current_price,
                            size_multiplier=policy['size_multiplier'],
                        )
                else:
                    # Legacy baskets: exact V1 recovery — no premise/factor
                    # gates, global layer cap, standard spacing, frozen ATR,
                    # full-size layers (C3).
                    next_layer = self.recovery.check_recovery_trigger(
                        basket, current_price, atr
                    )
                    if next_layer is not None:
                        self._add_recovery_layer(
                            basket, next_layer, balance, current_price
                        )

                self.database.update_basket(basket)
                remaining.append(basket)

            except Exception as e:
                logger.error(
                    'Error managing basket %s (%s): %s',
                    basket.id[:8], basket.symbol, e,
                )
                remaining.append(basket)

        return remaining

    # ───────────────────────────────────────────
    # V2 Lifecycle Helpers
    # ───────────────────────────────────────────

    def _get_live_atr(self, symbol: str) -> float:
        """Current 5m ATR for a symbol, cached with a short TTL.

        Returns 0.0 on failure so max(entry_atr, live_atr) degrades to the
        V1 frozen-ATR behaviour rather than blocking management.
        """
        now = time.time()
        cached = self._atr_cache.get(symbol)
        if cached and now - cached[0] < _ATR_CACHE_TTL_SECONDS:
            return cached[1]
        try:
            from signals.indicators import compute_atr

            df = self.exchange.fetch_ohlcv(
                symbol, self.settings.signal_timeframe, limit=60
            )
            atr_series = compute_atr(
                df['high'], df['low'], df['close'],
                period=self.settings.atr_period,
            ).dropna()
            value = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
            self._atr_cache[symbol] = (now, value)
            return value
        except Exception as e:
            logger.debug('Live ATR fetch failed for %s: %s', symbol, e)
            self._atr_cache[symbol] = (now, 0.0)
            return 0.0

    def _net_floor_roi(self, basket: Basket) -> float:
        """Minimum gross ROI (on margin) at which a market close realizes
        approximately >= 0 NET of round-trip costs (H2).

        Fees are charged on NOTIONAL while ROI thresholds are on MARGIN,
        so the cost expressed as ROI scales with leverage:
            (2 × taker_fee + exit slippage) × leverage
        e.g. at 8x with 0.04% taker and 0.05% slippage: ~1.04% of margin.

        Args:
            basket: The basket whose leverage determines the scaling.

        Returns:
            Fee-floor ROI as a fraction of basket margin.
        """
        per_notional = (
            2.0 * self.settings.taker_fee_pct + self.settings.slippage_pct
        )
        return per_notional * max(1, basket.leverage)

    def _premise_ok(self, basket: Basket) -> bool:
        """True while the symbol trend state does not oppose the basket.

        NEUTRAL/UNKNOWN states support both sides; only an opposing
        directional state invalidates the premise. Without a symbol state
        engine the premise is assumed valid (V1-equivalent).
        """
        if self.symbol_state_engine is None:
            return True
        snapshot = self.symbol_state_engine.get_or_classify(
            basket.symbol,
            lambda s: self.exchange.fetch_ohlcv(
                s, self.settings.trend_timeframe, limit=250
            ),
        )
        if snapshot is None:
            return True
        return snapshot.supports(basket.side)

    def _enter_wind_down(self, basket: Basket) -> None:
        """Transition a basket into wind-down (premise invalidated)."""
        basket.wind_down = True
        basket.wind_down_at = time.time()
        logger.warning(
            'WIND_DOWN: %s %s | premise invalidated — no new layers, exit at '
            'break-even-or-better within %.0fh | basket=%s',
            basket.side.upper(), basket.symbol,
            self.settings.wind_down_max_hours, basket.id[:8],
        )
        trade_logger.info(
            'WIND_DOWN %s %s | basket=%s',
            basket.side.upper(), basket.symbol, basket.id[:8],
        )

    # ───────────────────────────────────────────
    # Close Operations
    # ───────────────────────────────────────────

    def close_basket(self, basket: Basket, reason: str) -> Optional[TradeRecord]:
        """Close an entire basket — all active layers.

        Args:
            basket: The basket to close.
            reason: Reason for closure (for trade record).

        Returns:
            TradeRecord if successful, None on error.
        """
        try:
            total_qty = basket.total_quantity
            if total_qty <= 0:
                basket.close_all()
                self.database.close_basket(basket.id)
                return None

            ticker = self.exchange.fetch_ticker(basket.symbol)
            current_price = ticker['last']

            # Close position on exchange
            for attempt in range(3):
                try:
                    self.exchange.close_position(
                        basket.symbol, basket.side, total_qty
                    )
                    break
                except Exception as e:
                    # Position already flat on the exchange (liquidation, ADL,
                    # manual close): Binance rejects the reduce-only order with
                    # -2022. Without this, the basket row stayed 'active'
                    # forever — permanently pinning a position slot and
                    # blocking the symbol. Reconcile as externally closed.
                    msg = str(e).lower()
                    if '-2022' in msg or 'reduceonly order is rejected' in msg:
                        logger.warning(
                            'Basket %s %s already flat on exchange (%s) — '
                            'reconciling as external_close',
                            basket.symbol, basket.id[:8], e,
                        )
                        reason = 'external_close'
                        break
                    if attempt == 2:
                        logger.critical(
                            'FAILED to close basket %s after 3 attempts: %s',
                            basket.id[:8], e,
                        )
                        return None
                    logger.warning(
                        'Close attempt %d failed for %s: %s — retrying',
                        attempt + 1, basket.symbol, e,
                    )
                    time.sleep(1)

            # Calculate PnL
            unrealized = basket.unrealized_pnl(current_price)
            fee = total_qty * current_price * self.settings.taker_fee_pct * 2
            realized_pnl = unrealized - fee

            trade = TradeRecord(
                basket_id=basket.id,
                symbol=basket.symbol,
                side=basket.side,
                entry_price=basket.avg_entry_price,
                exit_price=current_price,
                quantity=total_qty,
                margin=basket.total_margin,
                leverage=basket.leverage,
                pnl=realized_pnl,
                fee=fee,
                layers_used=basket.layer_count,
                entry_time=basket.created_at,
                exit_time=time.time(),
                exit_reason=reason,
            )

            basket.close_all()
            self.database.close_basket(basket.id)
            self.database.save_trade(trade)

            # V2: feed the direction-aware post-loss response. Consecutive
            # losses on one side demote that side to SCOUT-only routing.
            # H3: the exit reason is passed so housekeeping losses
            # (time_triage / wind_down / break_even_exit) are not counted
            # as directional failures — only genuine stop-outs are.
            try:
                self.risk_manager.record_trade_result(
                    basket.side, realized_pnl, exit_reason=reason
                )
            except Exception as e:
                logger.debug('record_trade_result failed: %s', e)

            pnl_symbol = '+' if realized_pnl >= 0 else ''
            trade_logger.info(
                'CLOSE %s %s [%s] | entry=%.4f exit=%.4f | '
                'PnL=%s%.4f USDT | lev=%dx layers=%d margin=%.4f fee=%.4f | basket=%s',
                basket.side.upper(), basket.symbol, reason.upper(),
                trade.entry_price, current_price,
                pnl_symbol, realized_pnl, basket.leverage, trade.layers_used,
                trade.margin, fee, basket.id[:8],
            )

            return trade

        except Exception as e:
            logger.error('Error closing basket %s: %s', basket.id[:8], e)
            return None

    def close_all_baskets(
        self, baskets: List[Basket], reason: str
    ) -> List[TradeRecord]:
        """Emergency close all active baskets.

        Args:
            baskets: List of all baskets.
            reason: Reason for mass closure.

        Returns:
            List of TradeRecord for successful closures.
        """
        trades: List[TradeRecord] = []
        for basket in baskets:
            if basket.status == 'active':
                trade = self.close_basket(basket, reason)
                if trade:
                    trades.append(trade)
        return trades

    # ───────────────────────────────────────────
    # Internal Helpers
    # ───────────────────────────────────────────

    def _close_single_layer(
        self, basket: Basket, layer: RecoveryLayer, current_price: float
    ) -> None:
        """Close a single layer within a basket.

        Args:
            basket: Parent basket.
            layer: The layer to close.
            current_price: Current market price.
        """
        try:
            self.exchange.close_position(
                basket.symbol, basket.side, layer.quantity
            )
            layer.status = 'closed'
            trade_logger.info(
                'CLOSE LAYER L%d %s %s | entry=%.4f exit=%.4f | basket=%s',
                layer.layer_number, basket.side.upper(), basket.symbol,
                layer.entry_price, current_price, basket.id[:8],
            )
        except Exception as e:
            logger.error(
                'Failed to close L%d for %s: %s',
                layer.layer_number, basket.symbol, e,
            )

    def _add_recovery_layer(
        self, basket: Basket, layer_number: int,
        balance: float, current_price: float,
        size_multiplier: float = 1.0,
    ) -> None:
        """Add a recovery layer to an existing basket.

        Args:
            basket: The basket to add a layer to.
            layer_number: The layer number to add (2, 3, or 4).
            balance: Current account balance.
            current_price: Current market price.
            size_multiplier: Template size multiplier so SCOUT/RANGE layers
                stay proportional to their first-layer sizing.
        """
        try:
            vol = VolatilityLevel(basket.volatility)
        except ValueError:
            vol = VolatilityLevel.MEDIUM

        base_margin = self.position_sizer.calculate_base_margin(
            balance, vol, size_multiplier
        )
        layer_params = self.recovery.calculate_layer_params(
            basket, layer_number, base_margin, current_price, basket.leverage
        )

        # ── Per-basket hard margin cap ──
        # The total margin across ALL layers of a single basket may never exceed
        # the account-size hard cap (balance × margin_hard_cap_pct). This is the
        # primary guard against recovery layers compounding into excessive
        # margin on small accounts.
        hard_cap = self.settings.get_margin_hard_cap(balance)
        projected_basket_margin = basket.total_margin + layer_params.margin
        if projected_basket_margin > hard_cap:
            logger.info(
                'Recovery L%d blocked for %s: basket margin %.2f + %.2f = %.2f '
                'would exceed hard cap %.2f (balance=%.2f)',
                layer_number, basket.symbol, basket.total_margin,
                layer_params.margin, projected_basket_margin, hard_cap, balance,
            )
            return

        # Validate with market info
        market_info = self.exchange.get_symbol_info(basket.symbol)
        layer_params.quantity = round_quantity(layer_params.quantity, market_info)

        if layer_params.quantity <= 0:
            logger.warning('Recovery L%d qty rounded to 0 for %s', layer_number, basket.symbol)
            return

        if not validate_min_notional(
            layer_params.quantity, current_price, market_info
        ):
            logger.warning('Recovery L%d below min notional for %s', layer_number, basket.symbol)
            return

        # Risk check for the additional margin
        active_baskets = self.database.load_active_baskets()
        current_exposure = sum(b.total_margin for b in active_baskets)
        allowed, reason = self.risk_manager.can_open_position(
            layer_params.margin, balance, current_exposure, len(active_baskets)
        )
        if not allowed:
            logger.info(
                'Recovery L%d blocked for %s: %s', layer_number, basket.symbol, reason
            )
            return

        # Execute
        try:
            order_side = 'buy' if basket.side == 'long' else 'sell'
            order = self.exchange.place_market_order(
                basket.symbol, order_side, layer_params.quantity
            )

            fill_price = float(
                order.get('average', order.get('price', current_price)) or current_price
            )
            layer_params.entry_price = fill_price

            basket.add_layer(layer_params)
            self.database.update_basket(basket)

            trade_logger.info(
                'RECOVERY L%d %s %s | price=%.4f qty=%.8f margin=%.4f | basket=%s',
                layer_number, basket.side.upper(), basket.symbol,
                fill_price, layer_params.quantity, layer_params.margin,
                basket.id[:8],
            )

        except Exception as e:
            logger.error(
                'Failed to add recovery L%d for %s: %s',
                layer_number, basket.symbol, e,
            )
