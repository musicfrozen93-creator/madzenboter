"""
Zentry Futures Core — Backtest Engine.

Simulates the full trading loop bar-by-bar using historical data.
Applies the same risk management, recovery, TP/SL rules as live trading.
Includes slippage and fee simulation.
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.data_loader import DataLoader
from backtest.reporter import BacktestReporter
from config.settings import MarketRegime, Settings, VolatilityLevel
from core.models import Basket, RecoveryLayer, Signal, TradeRecord
from grid.recovery import RecoverySystem
from grid.take_profit import TakeProfitManager
from risk.position_sizer import PositionSizer
from risk.stop_loss import StopLossManager
from signals.indicators import compute_adx, compute_atr, compute_ema, compute_rsi

logger = logging.getLogger(__name__)


class _MockDatabase:
    """In-memory mock database for backtesting."""

    def __init__(self) -> None:
        self._state: Dict[str, str] = {}

    def set_state(self, key: str, value: str) -> None:
        self._state[key] = value

    def get_state(self, key: str) -> Optional[str]:
        return self._state.get(key)

    def save_daily_stats(self, stats: dict) -> None:
        pass  # Not persisted in backtests


class BacktestEngine:
    """Simulates the full trading loop bar-by-bar.

    Uses the same signal logic, recovery system, TP/SL rules,
    and risk management as the live engine, but operates on
    historical data with simulated fills.
    """

    def __init__(
        self, settings: Settings, initial_balance: float = 100.0
    ) -> None:
        self.settings = settings
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.equity_curve: List[float] = [initial_balance]
        self.trades: List[TradeRecord] = []
        self.active_baskets: List[Basket] = []

        # Components
        self.position_sizer = PositionSizer(settings)
        self.recovery = RecoverySystem(settings)
        self.tp_manager = TakeProfitManager(settings)
        self.sl_manager = StopLossManager(settings)

        # Risk tracking
        self._high_water_mark = initial_balance
        self._daily_start_balance = initial_balance
        self._current_date = ''
        self._daily_loss_triggered = False

    def run(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        data_loader: DataLoader,
    ) -> dict:
        """Run the complete backtest.

        Args:
            symbols: List of trading pairs to test.
            start_date: Start date 'YYYY-MM-DD'.
            end_date: End date 'YYYY-MM-DD'.
            data_loader: DataLoader instance.

        Returns:
            Dict of performance metrics.
        """
        logger.info(
            'Starting backtest: %s from %s to %s with $%.2f',
            ', '.join(symbols), start_date, end_date, self.initial_balance,
        )

        # Load data
        data_5m: Dict[str, pd.DataFrame] = {}
        data_1h: Dict[str, pd.DataFrame] = {}

        for symbol in symbols:
            df_5m = data_loader.load_ohlcv(symbol, '5m', start_date, end_date)
            df_1h = data_loader.load_ohlcv(symbol, '1h', start_date, end_date)
            if df_5m.empty:
                logger.warning('No 5m data for %s — skipping', symbol)
                continue
            data_5m[symbol] = df_5m.reset_index(drop=True)
            data_1h[symbol] = df_1h.reset_index(drop=True) if not df_1h.empty else pd.DataFrame()

        if not data_5m:
            logger.error('No data loaded for any symbol')
            return {}

        # Find the symbol with the most bars to drive the simulation
        primary_symbol = max(data_5m, key=lambda s: len(data_5m[s]))
        total_bars = len(data_5m[primary_symbol])
        logger.info('Simulating %d bars across %d symbols', total_bars, len(data_5m))

        # Pre-compute indicators for each symbol
        indicators: Dict[str, dict] = {}
        for symbol, df in data_5m.items():
            ind = {
                'rsi': compute_rsi(df['close'], self.settings.rsi_period),
                'atr': compute_atr(
                    df['high'], df['low'], df['close'], self.settings.atr_period
                ),
            }
            # EMA200 from 1h data
            if symbol in data_1h and not data_1h[symbol].empty:
                ind['ema200'] = compute_ema(
                    data_1h[symbol]['close'], self.settings.ema_period
                )
                ind['adx'] = compute_adx(
                    data_1h[symbol]['high'], data_1h[symbol]['low'],
                    data_1h[symbol]['close'], self.settings.adx_period
                )
            indicators[symbol] = ind

        # ── Main simulation loop ──
        scan_interval = max(1, self.settings.scan_interval_seconds // 300)  # bars

        for bar_idx in range(self.settings.ema_period + 50, total_bars):
            # Map current 5m bar timestamp to 1h bar index
            current_ts = data_5m[primary_symbol].iloc[bar_idx]['timestamp']

            # Check daily reset
            if hasattr(current_ts, 'strftime'):
                current_day = current_ts.strftime('%Y-%m-%d')
            else:
                current_day = str(current_ts)[:10]

            if current_day != self._current_date:
                self._current_date = current_day
                self._daily_start_balance = self.balance
                self._daily_loss_triggered = False

            # Daily loss check
            if self._daily_loss_triggered:
                self.equity_curve.append(self.balance)
                continue

            if self._daily_start_balance > 0:
                daily_pnl = (self.balance - self._daily_start_balance) / self._daily_start_balance
                if daily_pnl <= -self.settings.daily_loss_limit_pct:
                    self._close_all_baskets_sim(data_5m, bar_idx, 'daily_limit')
                    self._daily_loss_triggered = True
                    self.equity_curve.append(self.balance)
                    continue

            # Drawdown check
            if self._high_water_mark > 0:
                dd = (self._high_water_mark - self.balance) / self._high_water_mark
                if dd >= self.settings.max_drawdown_pct:
                    self._close_all_baskets_sim(data_5m, bar_idx, 'drawdown')
                    logger.info('Backtest stopped: max drawdown reached at bar %d', bar_idx)
                    break

            # Update HWM
            if self.balance > self._high_water_mark:
                self._high_water_mark = self.balance

            # ── Manage existing baskets ──
            for basket in list(self.active_baskets):
                if basket.symbol not in data_5m:
                    continue

                sym_df = data_5m[basket.symbol]
                if bar_idx >= len(sym_df):
                    continue

                bar = sym_df.iloc[bar_idx]
                current_price = float(bar['close'])
                bar_low = float(bar['low'])
                bar_high = float(bar['high'])
                atr = basket.atr_at_entry

                closed = False

                # Emergency SL
                worst_price = bar_low if basket.side == 'long' else bar_high
                if self.sl_manager.check_emergency_sl(basket, worst_price, self.balance):
                    self._close_basket_sim(basket, worst_price, 'emergency_sl')
                    closed = True
                elif self.sl_manager.check_basket_sl(basket, worst_price):
                    self._close_basket_sim(basket, worst_price, 'basket_sl')
                    closed = True

                if closed:
                    continue

                # Basket TP
                if self.tp_manager.check_basket_tp(basket, current_price):
                    self._close_basket_sim(basket, current_price, 'basket_tp')
                    continue

                # Individual layer checks
                for layer in list(basket.active_layers):
                    worst = bar_low if basket.side == 'long' else bar_high
                    if self.sl_manager.check_individual_sl(layer, worst, atr, basket.side):
                        layer.status = 'closed'
                    elif self.tp_manager.check_individual_tp(
                        layer, current_price, atr, basket.side
                    ):
                        layer.status = 'closed'

                if basket.layer_count == 0:
                    self._close_basket_sim(basket, current_price, 'individual_sl')
                    continue

                # Recovery layers
                recovery_layer = self.recovery.check_recovery_trigger(
                    basket, current_price, atr
                )
                if recovery_layer is not None:
                    self._add_recovery_sim(basket, recovery_layer, current_price)

            # ── Generate signals (every scan_interval bars) ──
            if bar_idx % scan_interval == 0:
                max_pos = self.position_sizer.get_max_positions(self.balance)
                current_symbols = {b.symbol for b in self.active_baskets}

                for symbol in data_5m:
                    if symbol in current_symbols:
                        continue
                    if len(self.active_baskets) >= max_pos:
                        break

                    signal = self._generate_signal_at_bar(
                        symbol, data_5m, data_1h, indicators, bar_idx
                    )
                    if signal:
                        self._open_position_sim(signal)
                        current_symbols.add(symbol)

            self.equity_curve.append(self.balance + self._unrealized_total(data_5m, bar_idx))

        # Close remaining positions
        self._close_all_baskets_sim(data_5m, total_bars - 1, 'backtest_end')

        # Generate report
        reporter = BacktestReporter()
        metrics = reporter.generate_report(
            self.trades, self.equity_curve, self.initial_balance
        )
        return metrics

    # ───────────────────────────────────────────
    # Simulation Helpers
    # ───────────────────────────────────────────

    def _generate_signal_at_bar(
        self,
        symbol: str,
        data_5m: Dict[str, pd.DataFrame],
        data_1h: Dict[str, pd.DataFrame],
        indicators: Dict[str, dict],
        bar_idx: int,
    ) -> Optional[Signal]:
        """Generate a signal using pre-computed indicators at a given bar.

        Args:
            symbol: Trading pair.
            data_5m: Dict of 5m DataFrames.
            data_1h: Dict of 1h DataFrames.
            indicators: Pre-computed indicator series.
            bar_idx: Current bar index in 5m data.

        Returns:
            Signal if conditions met, None otherwise.
        """
        if symbol not in data_5m or bar_idx >= len(data_5m[symbol]):
            return None

        ind = indicators.get(symbol, {})
        if 'rsi' not in ind or 'atr' not in ind:
            return None

        rsi = ind['rsi']
        atr = ind['atr']

        if bar_idx >= len(rsi) or pd.isna(rsi.iloc[bar_idx]):
            return None
        if bar_idx >= len(atr) or pd.isna(atr.iloc[bar_idx]):
            return None

        current_rsi = float(rsi.iloc[bar_idx])
        current_atr = float(atr.iloc[bar_idx])
        current_price = float(data_5m[symbol].iloc[bar_idx]['close'])

        # EMA200 from 1h: map 5m bar to 1h bar (ratio = 12)
        ema200_val = None
        if 'ema200' in ind and not ind['ema200'].empty:
            h_idx = bar_idx // 12
            ema_series = ind['ema200'].dropna()
            if h_idx < len(ind['ema200']) and not pd.isna(ind['ema200'].iloc[min(h_idx, len(ind['ema200']) - 1)]):
                ema200_val = float(ind['ema200'].iloc[min(h_idx, len(ind['ema200']) - 1)])

        if ema200_val is None:
            return None

        # Market classification
        regime = MarketRegime.UNKNOWN
        volatility = VolatilityLevel.MEDIUM

        if 'adx' in ind and not ind['adx'].empty:
            h_idx = min(bar_idx // 12, len(ind['adx']) - 1)
            if not pd.isna(ind['adx'].iloc[h_idx]):
                adx_val = float(ind['adx'].iloc[h_idx])
                if adx_val > self.settings.adx_trend_threshold:
                    regime = MarketRegime.TRENDING
                elif adx_val < self.settings.adx_sideways_threshold:
                    regime = MarketRegime.SIDEWAYS

        # ATR volatility
        if bar_idx >= 30:
            avg_atr = float(atr.iloc[bar_idx - 30:bar_idx].mean())
            if avg_atr > 0:
                ratio = current_atr / avg_atr
                if ratio > self.settings.high_vol_atr_multiplier:
                    volatility = VolatilityLevel.HIGH
                elif ratio < self.settings.low_vol_atr_multiplier:
                    volatility = VolatilityLevel.LOW

        # Entry conditions
        side = None
        strength = 0.0

        if current_price > ema200_val and current_rsi < self.settings.rsi_oversold:
            side = 'long'
            strength = max(0.1, min(1.0, (self.settings.rsi_oversold - current_rsi) / self.settings.rsi_oversold))
        elif current_price < ema200_val and current_rsi > self.settings.rsi_overbought:
            side = 'short'
            strength = max(0.1, min(1.0, (current_rsi - self.settings.rsi_overbought) / (100 - self.settings.rsi_overbought)))

        if side is None:
            return None

        return Signal(
            symbol=symbol, side=side, strength=strength, atr=current_atr,
            market_regime=regime.value, volatility=volatility.value,
            current_price=current_price, ema200=ema200_val, rsi=current_rsi,
        )

    def _open_position_sim(self, signal: Signal) -> None:
        """Simulate opening a position.

        Args:
            signal: Entry signal.
        """
        try:
            vol = VolatilityLevel(signal.volatility)
        except ValueError:
            vol = VolatilityLevel.MEDIUM

        leverage = self.settings.get_leverage(vol)
        base_margin = self.position_sizer.calculate_base_margin(self.balance, vol)
        margin = base_margin * self.settings.recovery_margin_multipliers[0]

        # Apply slippage
        if signal.side == 'long':
            fill_price = signal.current_price * (1 + self.settings.slippage_pct)
        else:
            fill_price = signal.current_price * (1 - self.settings.slippage_pct)

        notional = margin * leverage
        quantity = notional / fill_price
        fee = notional * self.settings.taker_fee_pct

        if margin + fee > self.balance * self.settings.max_exposure_pct:
            return

        self.balance -= fee

        layer = RecoveryLayer(
            layer_number=1, entry_price=fill_price,
            margin=margin, quantity=quantity, side=signal.side,
        )
        basket = Basket(
            symbol=signal.symbol, side=signal.side,
            atr_at_entry=signal.atr, volatility=signal.volatility,
            leverage=leverage,
        )
        basket.add_layer(layer)
        self.active_baskets.append(basket)

    def _add_recovery_sim(
        self, basket: Basket, layer_number: int, current_price: float
    ) -> None:
        """Simulate adding a recovery layer.

        Args:
            basket: The basket to add to.
            layer_number: Layer number (2, 3, or 4).
            current_price: Current market price.
        """
        try:
            vol = VolatilityLevel(basket.volatility)
        except ValueError:
            vol = VolatilityLevel.MEDIUM

        base_margin = self.position_sizer.calculate_base_margin(self.balance, vol)
        layer = self.recovery.calculate_layer_params(
            basket, layer_number, base_margin, current_price, basket.leverage
        )

        # Check exposure
        current_exposure = sum(b.total_margin for b in self.active_baskets)
        if (current_exposure + layer.margin) / self.balance > self.settings.max_exposure_pct:
            return

        fee = layer.quantity * current_price * self.settings.taker_fee_pct
        self.balance -= fee
        basket.add_layer(layer)

    def _close_basket_sim(
        self, basket: Basket, exit_price: float, reason: str
    ) -> None:
        """Simulate closing a basket.

        Args:
            basket: The basket to close.
            exit_price: Exit price.
            reason: Reason for closure.
        """
        # Apply slippage on exit
        if basket.side == 'long':
            exit_price = exit_price * (1 - self.settings.slippage_pct)
        else:
            exit_price = exit_price * (1 + self.settings.slippage_pct)

        total_qty = basket.total_quantity
        unrealized = basket.unrealized_pnl(exit_price)
        fee = total_qty * exit_price * self.settings.taker_fee_pct
        realized = unrealized - fee

        self.balance += realized

        trade = TradeRecord(
            basket_id=basket.id, symbol=basket.symbol, side=basket.side,
            entry_price=basket.avg_entry_price, exit_price=exit_price,
            quantity=total_qty, margin=basket.total_margin,
            leverage=basket.leverage, pnl=realized, fee=fee,
            layers_used=basket.layer_count, entry_time=basket.created_at,
            exit_time=time.time(), exit_reason=reason,
        )
        self.trades.append(trade)
        basket.close_all()

        if basket in self.active_baskets:
            self.active_baskets.remove(basket)

    def _close_all_baskets_sim(
        self, data_5m: Dict[str, pd.DataFrame], bar_idx: int, reason: str
    ) -> None:
        """Simulate closing all active baskets.

        Args:
            data_5m: Dict of 5m DataFrames.
            bar_idx: Current bar index.
            reason: Reason for mass closure.
        """
        for basket in list(self.active_baskets):
            if basket.symbol in data_5m and bar_idx < len(data_5m[basket.symbol]):
                price = float(data_5m[basket.symbol].iloc[bar_idx]['close'])
            else:
                price = basket.avg_entry_price
            self._close_basket_sim(basket, price, reason)

    def _unrealized_total(
        self, data_5m: Dict[str, pd.DataFrame], bar_idx: int
    ) -> float:
        """Calculate total unrealised PnL across all active baskets.

        Args:
            data_5m: Dict of 5m DataFrames.
            bar_idx: Current bar index.

        Returns:
            Total unrealised PnL.
        """
        total = 0.0
        for basket in self.active_baskets:
            if basket.symbol in data_5m and bar_idx < len(data_5m[basket.symbol]):
                price = float(data_5m[basket.symbol].iloc[bar_idx]['close'])
                total += basket.unrealized_pnl(price)
        return total
