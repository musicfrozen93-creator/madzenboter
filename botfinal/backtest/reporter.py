"""
Zentry Futures Core — Backtest Reporter.

Generates comprehensive performance metrics from backtest results
including Sharpe ratio, profit factor, max drawdown, and more.
"""

import json
import logging
import math
from typing import List

import numpy as np

from core.models import TradeRecord

logger = logging.getLogger(__name__)


class BacktestReporter:
    """Generates and formats backtest performance reports."""

    def generate_report(
        self,
        trades: List[TradeRecord],
        equity_curve: List[float],
        initial_balance: float,
    ) -> dict:
        """Generate comprehensive backtest metrics.

        Args:
            trades: List of completed trades.
            equity_curve: Balance at each checkpoint.
            initial_balance: Starting balance.

        Returns:
            Dict with all performance metrics.
        """
        total_trades = len(trades)
        final_balance = equity_curve[-1] if equity_curve else initial_balance

        net_profit = final_balance - initial_balance
        roi = (net_profit / initial_balance * 100) if initial_balance > 0 else 0

        winning_trades = [t for t in trades if t.pnl > 0]
        losing_trades = [t for t in trades if t.pnl <= 0]
        win_count = len(winning_trades)
        lose_count = len(losing_trades)
        win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0

        gross_profit = sum(t.pnl for t in winning_trades)
        gross_loss = abs(sum(t.pnl for t in losing_trades))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

        avg_win = (gross_profit / win_count) if win_count > 0 else 0
        avg_loss = (gross_loss / lose_count) if lose_count > 0 else 0

        # Max drawdown from equity curve
        max_dd, max_dd_pct = self._calculate_max_drawdown(equity_curve)

        # Sharpe ratio (annualised, daily returns, rf=0)
        sharpe = self._calculate_sharpe(equity_curve)

        # Average trade duration (hours)
        durations = [
            (t.exit_time - t.entry_time) / 3600
            for t in trades if t.exit_time > t.entry_time
        ]
        avg_duration = np.mean(durations) if durations else 0

        # Average recovery layers used
        layers_used = [t.layers_used for t in trades]
        avg_layers = np.mean(layers_used) if layers_used else 0

        # Total fees
        total_fees = sum(t.fee for t in trades)

        # Exit reason distribution
        exit_reasons = {}
        for t in trades:
            exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

        metrics = {
            'initial_balance': round(initial_balance, 2),
            'final_balance': round(final_balance, 2),
            'net_profit': round(net_profit, 4),
            'roi_pct': round(roi, 2),
            'total_trades': total_trades,
            'winning_trades': win_count,
            'losing_trades': lose_count,
            'win_rate_pct': round(win_rate, 2),
            'gross_profit': round(gross_profit, 4),
            'gross_loss': round(gross_loss, 4),
            'profit_factor': round(profit_factor, 4) if profit_factor != float('inf') else 999.0,
            'avg_win': round(avg_win, 4),
            'avg_loss': round(avg_loss, 4),
            'max_drawdown': round(max_dd, 4),
            'max_drawdown_pct': round(max_dd_pct, 2),
            'sharpe_ratio': round(sharpe, 4),
            'avg_trade_duration_hours': round(float(avg_duration), 2),
            'avg_layers_used': round(float(avg_layers), 2),
            'total_fees': round(total_fees, 4),
            'exit_reasons': exit_reasons,
        }

        return metrics

    def print_report(self, metrics: dict) -> None:
        """Print a formatted backtest report to console.

        Args:
            metrics: Dict of performance metrics.
        """
        pnl_sign = '+' if metrics['net_profit'] >= 0 else ''

        report = f"""
╔══════════════════════════════════════════════════╗
║      ZENTRY FUTURES CORE — BACKTEST REPORT       ║
╠══════════════════════════════════════════════════╣
║                                                  ║
║  Initial Balance:    ${metrics['initial_balance']:>10.2f}              ║
║  Final Balance:      ${metrics['final_balance']:>10.2f}              ║
║  Net Profit:       {pnl_sign}${metrics['net_profit']:>10.4f} ({pnl_sign}{metrics['roi_pct']:.2f}%)      ║
║                                                  ║
╠══════════════════════════════════════════════════╣
║  TRADING STATISTICS                              ║
╠══════════════════════════════════════════════════╣
║  Total Trades:       {metrics['total_trades']:>6d}                    ║
║  Winning Trades:     {metrics['winning_trades']:>6d}                    ║
║  Losing Trades:      {metrics['losing_trades']:>6d}                    ║
║  Win Rate:           {metrics['win_rate_pct']:>6.2f}%                   ║
║  Profit Factor:      {metrics['profit_factor']:>8.4f}                 ║
║  Avg Win:           ${metrics['avg_win']:>10.4f}              ║
║  Avg Loss:          ${metrics['avg_loss']:>10.4f}              ║
║                                                  ║
╠══════════════════════════════════════════════════╣
║  RISK METRICS                                    ║
╠══════════════════════════════════════════════════╣
║  Sharpe Ratio:       {metrics['sharpe_ratio']:>8.4f}                 ║
║  Max Drawdown:       {metrics['max_drawdown_pct']:>6.2f}%                   ║
║  Max DD Amount:     ${metrics['max_drawdown']:>10.4f}              ║
║                                                  ║
╠══════════════════════════════════════════════════╣
║  EXECUTION DETAILS                               ║
╠══════════════════════════════════════════════════╣
║  Avg Duration:       {metrics['avg_trade_duration_hours']:>6.2f} hours              ║
║  Avg Layers Used:    {metrics['avg_layers_used']:>6.2f}                    ║
║  Total Fees:        ${metrics['total_fees']:>10.4f}              ║
║                                                  ║
╚══════════════════════════════════════════════════╝
"""
        print(report)

        if metrics.get('exit_reasons'):
            print('  Exit Reasons:')
            for reason, count in sorted(
                metrics['exit_reasons'].items(), key=lambda x: x[1], reverse=True
            ):
                print(f'    {reason:<20s} {count:>4d}')
            print()

    def save_report(self, metrics: dict, filepath: str) -> None:
        """Save report metrics to a JSON file.

        Args:
            metrics: Dict of performance metrics.
            filepath: Output file path.
        """
        import os
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(metrics, f, indent=2)
        logger.info('Backtest report saved to %s', filepath)

    # ───────────────────────────────────────────
    # Calculation Helpers
    # ───────────────────────────────────────────

    def _calculate_max_drawdown(
        self, equity_curve: List[float]
    ) -> tuple[float, float]:
        """Calculate maximum drawdown from equity curve.

        Args:
            equity_curve: List of balance values.

        Returns:
            Tuple of (max_drawdown_amount, max_drawdown_percent).
        """
        if not equity_curve or len(equity_curve) < 2:
            return 0.0, 0.0

        peak = equity_curve[0]
        max_dd = 0.0
        max_dd_pct = 0.0

        for value in equity_curve:
            if value > peak:
                peak = value
            dd = peak - value
            dd_pct = (dd / peak * 100) if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct

        return max_dd, max_dd_pct

    def _calculate_sharpe(self, equity_curve: List[float]) -> float:
        """Calculate annualised Sharpe ratio from equity curve.

        Uses daily returns with risk-free rate = 0.

        Args:
            equity_curve: List of balance values.

        Returns:
            Annualised Sharpe ratio.
        """
        if len(equity_curve) < 3:
            return 0.0

        # Sample returns at ~daily intervals (every 288 5m bars)
        returns = []
        step = max(1, len(equity_curve) // max(2, len(equity_curve) // 288))
        for i in range(step, len(equity_curve), step):
            prev = equity_curve[i - step]
            if prev > 0:
                returns.append((equity_curve[i] - prev) / prev)

        if len(returns) < 2:
            return 0.0

        avg_return = np.mean(returns)
        std_return = np.std(returns, ddof=1)

        if std_return == 0:
            return 0.0

        sharpe = (avg_return / std_return) * math.sqrt(365)
        return float(sharpe)
