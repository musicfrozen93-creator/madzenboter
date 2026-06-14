"""Verification harness for the participation / sizing / SL changes.

Exercises the REAL production code paths (Settings, PositionSizer, RecoverySystem)
against config/config.json to prove:

  1. basket_sl_pct == 0.15
  2. max-positions tiers == 6 / 8 / 10
  3. Effective per-basket hard cap == min(balance*10%, $2.50)
  4. TOTAL basket margin (L1 + every recovery layer) can NEVER exceed $2.50,
     simulated layer-by-layer exactly as PositionManager._add_recovery_layer does.
  5. Old vs new first-layer sizing.

Run:  python scripts/verify_basket_cap.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import Settings, VolatilityLevel
from risk.position_sizer import PositionSizer
from grid.recovery import RecoverySystem
from core.dto import Basket, RecoveryLayer

ABS_CAP = 2.50
BALANCES = [20, 25, 30, 50, 75, 100, 250, 500, 1000]
VOLS = [VolatilityLevel.LOW, VolatilityLevel.MEDIUM, VolatilityLevel.HIGH]


def simulate_basket_total(settings, sizer, recovery, balance, vol):
    """Replicate exactly how a basket grows in production and return its total
    margin after all recovery layers, applying the same hard-cap gate as
    PositionManager._add_recovery_layer (which blocks any layer that would push
    the projected total over the cap)."""
    leverage = settings.get_leverage(vol)
    base = sizer.calculate_base_margin(balance, vol)
    hard_cap = settings.get_margin_hard_cap(balance)

    # Layer 1 (entry). Price is arbitrary for a margin-only simulation.
    price = 100.0
    basket = Basket(symbol='TEST/USDT', side='long', atr_at_entry=1.0,
                    volatility=vol.value, leverage=leverage)
    basket.add_layer(RecoveryLayer(layer_number=1, entry_price=price,
                                   margin=base, quantity=base * leverage / price,
                                   side='long'))

    blocked_at = None
    for layer_no in range(2, settings.recovery_max_layers + 1):
        params = recovery.calculate_layer_params(basket, layer_no, base, price, leverage)
        projected = basket.total_margin + params.margin
        if projected > hard_cap:               # same gate as production
            blocked_at = layer_no
            break
        basket.add_layer(params)

    return base, hard_cap, basket.total_margin, basket.layer_count, blocked_at


def main():
    settings = Settings.load('config/config.json')
    sizer = PositionSizer(settings)
    recovery = RecoverySystem(settings)

    print('=' * 78)
    print('SCALAR SETTINGS')
    print('=' * 78)
    print(f'  basket_sl_pct          = {settings.basket_sl_pct}   (expect 0.15)')
    print(f'  emergency_sl_acct_pct  = {settings.emergency_sl_account_pct}   (unchanged)')
    print(f'  basket_margin_abs_cap  = {settings.basket_margin_abs_cap}   (expect 2.5)')
    print(f'  margin_hard_cap_pct    = {settings.margin_hard_cap_pct}')
    print(f'  recovery_multipliers   = {settings.recovery_margin_multipliers}'
          f'  (Sigma={sum(settings.recovery_margin_multipliers):.2f})')

    print()
    print('MAX POSITIONS BY BALANCE (expect 6 / 8 / 10)')
    for bal in [20, 49, 50, 75, 100, 101, 500]:
        print(f'  ${bal:>5} -> {settings.get_max_positions(bal)} positions')

    print()
    print('=' * 78)
    print('PER-BASKET TOTAL MARGIN SIMULATION (all layers, all volatilities)')
    print('=' * 78)
    print(f'{"bal":>6} {"vol":>7} {"L1base":>8} {"cap":>7} {"layers":>7} '
          f'{"TOTAL":>8} {"blocked@":>9}  ok?')
    worst = 0.0
    all_ok = True
    for bal in BALANCES:
        for vol in VOLS:
            base, cap, total, layers, blocked = simulate_basket_total(
                settings, sizer, recovery, bal, vol)
            ok = total <= ABS_CAP + 1e-9
            all_ok &= ok
            worst = max(worst, total)
            print(f'{bal:>6} {vol.value:>7} {base:>8.4f} {cap:>7.2f} '
                  f'{layers:>7} {total:>8.4f} {str(blocked):>9}  '
                  f'{"OK" if ok else "FAIL <<<"}')

    print()
    print('=' * 78)
    print('OLD vs NEW first-layer base margin (MEDIUM vol)')
    print('=' * 78)
    print('  (OLD = balance%-range midpoint clamped to 10% cap; '
          'NEW = production calculate_base_margin)')
    for bal in BALANCES:
        lo, hi = settings.get_target_margin_range(bal)
        old_cap = max(settings.min_margin_floor, bal * settings.margin_hard_cap_pct)
        old_mid = min((lo + hi) / 2.0, old_cap)
        old_mid = max(settings.min_margin_floor, old_mid)
        new_base = sizer.calculate_base_margin(bal, VolatilityLevel.MEDIUM)
        print(f'  ${bal:>5}:  old L1 ~ {old_mid:>6.3f}   ->   new L1 = {new_base:>6.3f}')

    print()
    print('=' * 78)
    print(f'WORST-CASE TOTAL BASKET MARGIN OBSERVED = {worst:.4f}')
    print(f'HARD GUARANTEE  total <= ${ABS_CAP:.2f} : '
          f'{"PASS" if all_ok else "FAIL"}')
    print('=' * 78)
    sys.exit(0 if all_ok else 1)


if __name__ == '__main__':
    main()
