"""
Zentry Futures Core — Position Sizer.

Dynamic position sizing based on account balance tier and volatility.
Prioritises capital preservation over aggressive sizing.
"""

import logging

from config.settings import Settings, VolatilityLevel
from exchange.utils import round_quantity

logger = logging.getLogger(__name__)


class PositionSizer:
    """Calculates position size (margin and quantity) for new entries.

    Account-size-aware, percentage-based sizing (capital preservation first):
      • First-layer target margin = balance × margin_target_pct_range
        (HIGH volatility uses the low end, LOW uses the high end).
      • Every entry is hard-capped at balance × margin_hard_cap_pct so a single
        basket can never consume more than that fraction of the account.
      • evaluate_entry() additionally rejects symbols whose smallest valid order
        (min-notional / min-lot) would breach the hard cap, or whose leverage
        would place liquidation too close.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def calculate_base_margin(
        self, balance: float, volatility: VolatilityLevel
    ) -> float:
        """Calculate base (first-layer) margin for a new position.

        Percentage of balance, adjusted by volatility, clamped to the dust
        floor and the per-basket hard cap.

        Args:
            balance: Current account balance in USDT.
            volatility: Current volatility classification.

        Returns:
            Base margin amount in USDT.
        """
        lo, hi = self.settings.get_target_margin_range(balance)

        # Volatility selects within the range: HIGH = conservative (low end),
        # LOW = slightly more aggressive (high end), MEDIUM = midpoint.
        if volatility == VolatilityLevel.HIGH:
            base = lo
        elif volatility == VolatilityLevel.LOW:
            base = hi
        else:
            base = (lo + hi) / 2.0

        hard_cap = self.settings.get_margin_hard_cap(balance)

        # ── Progression-fit clamp ──
        # Size the first layer so the ENTIRE recovery progression fits within the
        # per-basket hard cap: base × Σ(recovery_margin_multipliers) ≤ hard_cap.
        # This keeps total basket margin bounded by the cap even after all layers
        # are added, so recovery never compounds a basket past the ceiling while
        # still allowing every layer to fire. The recovery multipliers and ATR
        # trigger distances themselves are unchanged — only the base is scaled.
        multipliers = self.settings.recovery_margin_multipliers
        progression_sum = sum(multipliers) if multipliers else 1.0
        if progression_sum > 0:
            # Truncate (floor) to 4dp so the FINAL rounded base still satisfies
            # base × Σ ≤ hard_cap. Rounding up here would tip the last layer one
            # hair over the cap and the recovery gate would needlessly drop it.
            max_base = int((hard_cap / progression_sum) * 1e4) / 1e4
            base = min(base, max_base)

        # Dust floor and absolute per-basket hard cap.
        base = max(self.settings.min_margin_floor, base)
        base = min(base, hard_cap)

        logger.debug(
            'Position size: balance=%.2f vol=%s → margin=%.4f '
            '(target=%.2f–%.2f progression_sum=%.2f hard_cap=%.2f)',
            balance, volatility.value, base, lo, hi, progression_sum, hard_cap,
        )
        return round(base, 4)

    def estimate_liquidation_distance_pct(self, leverage: int) -> float:
        """Estimate price distance (fraction) from entry to liquidation.

        Approximation for cross/isolated futures: 1/leverage minus the
        maintenance-margin rate. Higher leverage → liquidation is closer.

        Args:
            leverage: Leverage multiplier.

        Returns:
            Estimated liquidation distance as a fraction of entry price.
        """
        if leverage <= 0:
            return 0.0
        return max(0.0, (1.0 / leverage) - self.settings.maintenance_margin_rate)

    def evaluate_entry(
        self,
        balance: float,
        price: float,
        leverage: int,
        volatility: VolatilityLevel,
        market_info: dict,
    ) -> dict:
        """Build a realistic order plan and judge its suitability for the account.

        Accounts for the exchange's min-notional and min-lot constraints, so the
        returned margin/notional reflect the SMALLEST order that would actually
        fill — not just the ideal target. Rejects the symbol when that smallest
        order would breach the account-size hard cap or sit too close to
        liquidation.

        Returns a dict:
            quantity, margin, notional, liquidation_distance_pct, leverage,
            base_margin, hard_cap, suitable (bool), reason (str).
        """
        base_margin = self.calculate_base_margin(balance, volatility)
        hard_cap = self.settings.get_margin_hard_cap(balance)

        result = {
            'quantity': 0.0, 'margin': 0.0, 'notional': 0.0,
            'liquidation_distance_pct': 0.0, 'leverage': leverage,
            'base_margin': base_margin, 'hard_cap': hard_cap,
            'suitable': False, 'reason': 'unknown',
        }

        if price <= 0 or leverage <= 0:
            result['reason'] = 'invalid price/leverage'
            return result

        limits = market_info.get('limits', {})
        min_notional = limits.get('cost', {}).get('min') or 5.0
        min_qty = limits.get('amount', {}).get('min') or 0.0

        # Ideal quantity for the target margin, floored to the lot step.
        target_notional = base_margin * leverage
        target_qty = round_quantity(target_notional / price, market_info)

        # Smallest quantity that satisfies BOTH min-notional and min-lot.
        min_qty_for_notional = min_notional / price
        required_qty = max(target_qty, min_qty, min_qty_for_notional)
        required_qty = round_quantity(required_qty, market_info)

        # Flooring can drop a sub-step quantity to zero, or just under
        # min-notional — nudge up by one lot step in that case.
        if required_qty <= 0 or required_qty * price < min_notional:
            step = self._lot_step(market_info)
            required_qty = round_quantity(required_qty + step, market_info)

        notional = required_qty * price
        margin = notional / leverage
        liq_dist = self.estimate_liquidation_distance_pct(leverage)

        result.update({
            'quantity': required_qty,
            'margin': round(margin, 4),
            'notional': round(notional, 4),
            'liquidation_distance_pct': round(liq_dist, 4),
        })

        # ── Suitability gates ──
        if required_qty <= 0:
            result['reason'] = 'quantity rounds to zero'
        elif notional < min_notional * 0.999:
            result['reason'] = f'below min notional ({notional:.2f} < {min_notional:.2f})'
        elif margin > hard_cap:
            result['reason'] = (
                f'required margin {margin:.2f} exceeds account hard cap '
                f'{hard_cap:.2f} (balance={balance:.2f})'
            )
        elif liq_dist < self.settings.min_liquidation_distance_pct:
            result['reason'] = (
                f'liquidation distance {liq_dist:.1%} < minimum '
                f'{self.settings.min_liquidation_distance_pct:.1%} at {leverage}x'
            )
        else:
            result['suitable'] = True
            result['reason'] = 'OK'

        return result

    @staticmethod
    def _lot_step(market_info: dict) -> float:
        """Return the lot step size, defaulting to a tiny step when unknown."""
        precision = market_info.get('precision', {}).get('amount', 8)
        if isinstance(precision, float) and precision > 0:
            return precision
        try:
            return 10 ** (-int(precision))
        except (TypeError, ValueError):
            return 1e-8

    def calculate_quantity(
        self, margin: float, price: float, leverage: int, market_info: dict
    ) -> float:
        """Calculate order quantity from margin, price, and leverage.

        Args:
            margin: Margin to allocate in USDT.
            price: Current market price.
            leverage: Leverage multiplier.
            market_info: CCXT market info dict for precision.

        Returns:
            Quantity rounded down to exchange precision.
        """
        if price <= 0 or leverage <= 0:
            return 0.0

        notional = margin * leverage
        quantity = notional / price
        quantity = round_quantity(quantity, market_info)
        return quantity

    def get_max_positions(self, balance: float) -> int:
        """Maximum simultaneous baskets allowed for the balance.

        Args:
            balance: Current account balance.

        Returns:
            Maximum position count.
        """
        return self.settings.get_max_positions(balance)
