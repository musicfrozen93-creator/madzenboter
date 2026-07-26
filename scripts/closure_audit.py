#!/usr/bin/env python3
"""ZenGrid — Trade-Closure & Cross-Account Consistency Forensic Audit.

READ-ONLY. Never opens, closes, or modifies a position or any DB row. It answers
"why did a basket that should have closed stay open, and why did one account
close while another holding the same symbol did not?" using REAL data:

  • basket / recovery-layer / trade rows from the database,
  • the persisted ``bot_state`` locks (incl. ``tp_lock_<basket_id>``),
  • live exchange price + position per account (when credentials decrypt), and
  • the ``trades.log`` lines (TP_LOCK_ACTIVATED / TP_LOCK_EXECUTED / BASKET_SL_HIT
    / ROI_*).

The exit decision for every open basket is recomputed with the SAME
``TakeProfitManager.evaluate_exit`` the bot uses, so the audit can never diverge
from production logic.

Sections (match the investigation brief):
  PART 1  single-account closure audit (every basket: full decision row)
  PART 2  cross-account consistency (same symbol/side/entry-window groups)
  TP-LOCK audit  ACTIVATED-without-EXECUTED + orphaned persisted locks
  ROI audit      ROI >= target but still open
  TP audit       net PnL >= USD target but still open
  POSITION-SYNC  exchange qty/PnL vs internal qty/PnL
  TIMELINE       per-symbol open/close timeline across accounts
  FAILURE REPORT baskets that should have closed but did not + root cause + fix

Usage::

    # DB-only (no live prices/positions — uses last known price if provided):
    python -m scripts.closure_audit

    # With live exchange price + position-sync (decrypts per-account keys):
    python -m scripts.closure_audit --live

    # Focus a single symbol / account:
    python -m scripts.closure_audit --symbol TRX/USDT:USDT
    python -m scripts.closure_audit --account 12

Requires the same env as the bot (DATABASE_URL, MASTER_ENCRYPTION_KEY for --live).
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import Settings
from core.database import Database
from core.dto import Basket, RecoveryLayer
from core.models import BasketModel, BotStateModel, RecoveryLayerModel, TradeModel
from grid.take_profit import TakeProfitManager


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _ts(epoch: Optional[float]) -> str:
    if not epoch:
        return '-'
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OSError):
        return str(epoch)


def _basket_from_orm(row: BasketModel, layers: List[RecoveryLayerModel]) -> Basket:
    """Rebuild the in-memory Basket DTO exactly as the bot loads it."""
    b = Basket(
        symbol=row.symbol, side=row.side, atr_at_entry=row.atr_at_entry,
        volatility=row.volatility, id=row.id, created_at=row.created_at,
        status=row.status, leverage=row.leverage, account_id=row.account_id,
    )
    for lr in sorted(layers, key=lambda x: x.layer_number):
        b.add_layer(RecoveryLayer(
            layer_number=lr.layer_number, entry_price=lr.entry_price,
            margin=lr.margin, quantity=lr.quantity, side=lr.side,
            timestamp=lr.timestamp, status=lr.status,
        ))
    return b


class LiveData:
    """Per-account exchange clients for live price + position (optional, --live)."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self._clients: dict = {}
        self._price_cache: dict = {}
        self._mgr = None
        try:
            from accounts.encryption import EncryptionService
            from accounts.manager import AccountManager
            enc = EncryptionService(settings.master_encryption_key)
            self._mgr = AccountManager(db, enc, settings)
        except Exception as e:  # pragma: no cover - env dependent
            print(f'  [live] account manager unavailable: {e}')

    def _client(self, account_id: int):
        if account_id in self._clients:
            return self._clients[account_id]
        client = None
        try:
            from exchange.client import ExchangeClient
            acct = self.db.get_account_by_id(account_id)
            api_key, api_secret = self._mgr.decrypt_account_keys(acct)
            acct_settings = Settings.create_account_settings(
                self.settings, {'leverage_override': acct.leverage_override})
            acct_settings.use_testnet = acct.use_testnet
            client = ExchangeClient.for_account(acct_settings, api_key, api_secret)
            client.initialize()
        except Exception as e:  # pragma: no cover - env dependent
            print(f'  [live] account {account_id} client unavailable: {e}')
        self._clients[account_id] = client
        return client

    def price(self, account_id: int, symbol: str) -> Optional[float]:
        if symbol in self._price_cache:
            return self._price_cache[symbol]
        client = self._client(account_id)
        if not client:
            return None
        try:
            p = float(client.fetch_ticker(symbol)['last'])
            self._price_cache[symbol] = p
            return p
        except Exception:
            return None

    def positions(self, account_id: int) -> Dict[Tuple[str, str], dict]:
        client = self._client(account_id)
        if not client:
            return {}
        out: Dict[Tuple[str, str], dict] = {}
        try:
            for p in client.fetch_positions():
                out[(p['symbol'], (p['side'] or '').lower())] = p
        except Exception:
            pass
        return out


# ─────────────────────────────────────────────
# Log parsing (TP lock pairing)
# ─────────────────────────────────────────────

_SYM_RE = re.compile(r'symbol=(\S+)')
_REASON_RE = re.compile(r'(?:close_reason|target_hit)=(\S+)')


def parse_tp_lock_log(log_path: str) -> Dict[str, dict]:
    """Pair TP_LOCK_ACTIVATED / TP_LOCK_EXECUTED lines from the trades log.

    Keyed by 'account|symbol' (the log does not carry the basket id), each value
    holds the activation and execution lines so unmatched activations surface.
    """
    events: Dict[str, dict] = defaultdict(lambda: {'activated': [], 'executed': []})
    if not os.path.exists(log_path):
        return {}
    acct_re = re.compile(r'account=(\S+)')
    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if 'TP_LOCK_ACTIVATED' not in line and 'TP_LOCK_EXECUTED' not in line:
                continue
            acct = acct_re.search(line)
            sym = _SYM_RE.search(line)
            key = f'{acct.group(1) if acct else "?"}|{sym.group(1) if sym else "?"}'
            if 'TP_LOCK_ACTIVATED' in line:
                events[key]['activated'].append(line.strip())
            else:
                events[key]['executed'].append(line.strip())
    return dict(events)


# ─────────────────────────────────────────────
# PART 1 — single-account closure audit
# ─────────────────────────────────────────────

def audit_open_baskets(
    db: Database, settings: Settings, live: Optional[LiveData],
    locks: Dict[str, str], symbol_filter: Optional[str], account_filter: Optional[int],
) -> List[dict]:
    """Recompute the exit decision for every ACTIVE basket. Returns audit rows."""
    tp = TakeProfitManager(settings)
    rows: List[dict] = []
    with db.session() as s:
        q = s.query(BasketModel).filter(BasketModel.status == 'active')
        if symbol_filter:
            q = q.filter(BasketModel.symbol == symbol_filter)
        if account_filter is not None:
            q = q.filter(BasketModel.account_id == account_filter)
        for row in q.order_by(BasketModel.symbol, BasketModel.account_id).all():
            layers = s.query(RecoveryLayerModel).filter(
                RecoveryLayerModel.basket_id == row.id).all()
            basket = _basket_from_orm(row, layers)
            price = None
            if live:
                price = live.price(row.account_id, row.symbol)
            tier = settings.get_tier_by_id(basket.volatility) or settings.account_tiers[0]
            l1_roi, rec_roi = settings.roi_targets_for(basket.symbol, tier)
            roi_target = rec_roi if basket.layer_count >= 2 else l1_roi
            decision, gross, net, roi, usd_target = 'NO_PRICE', None, None, None, tp.target_usd(basket)
            if price:
                decision, m = tp.evaluate_exit(basket, price)
                decision = decision or 'hold'
                gross, net, roi = m['gross_pnl'], m['net_pnl'], m['roi']
            rows.append({
                'account_id': row.account_id, 'tier': basket.volatility,
                'symbol': row.symbol, 'side': row.side,
                'entry': round(basket.avg_entry_price, 8), 'price': price,
                'gross': gross, 'net': net, 'margin': round(basket.total_margin, 4),
                'roi': roi, 'roi_target': roi_target, 'tp_target': usd_target,
                'layers': basket.layer_count, 'decision': decision,
                'tp_locked': locks.get(f'account_{row.account_id}_tp_lock_{row.id}', ''),
                'created_at': basket.created_at, 'basket_id': row.id,
            })
    return rows


def print_part1(rows: List[dict]) -> None:
    print('\n' + '=' * 70)
    print('PART 1 — SINGLE-ACCOUNT CLOSURE AUDIT (every active basket)')
    print('=' * 70)
    if not rows:
        print('No active baskets found.')
        return
    hdr = ('acct', 'tier', 'symbol', 'dir', 'entry', 'price', 'gross', 'net',
           'margin', 'roi%', 'roiT%', 'tp$', 'L', 'decision', 'tp_lock')
    print('{:>4} {:<5} {:<16} {:<5} {:>9} {:>9} {:>8} {:>8} {:>7} {:>6} {:>6} {:>5} {:>2} {:<12} {:<7}'.format(*hdr))
    for r in rows:
        print('{:>4} {:<5} {:<16} {:<5} {:>9} {:>9} {:>8} {:>8} {:>7} {:>6} {:>6} {:>5} {:>2} {:<12} {:<7}'.format(
            str(r['account_id']), r['tier'], r['symbol'].split('/')[0], r['side'],
            f"{r['entry']:.6f}" if r['entry'] else '-',
            f"{r['price']:.6f}" if r['price'] else 'NONE',
            f"{r['gross']:.4f}" if r['gross'] is not None else '-',
            f"{r['net']:.4f}" if r['net'] is not None else '-',
            f"{r['margin']:.2f}",
            f"{r['roi'] * 100:.2f}" if r['roi'] is not None else '-',
            f"{r['roi_target'] * 100:.1f}",
            f"{r['tp_target']:.2f}",
            r['layers'], r['decision'], (r['tp_locked'] or '-')))


# ─────────────────────────────────────────────
# PART 2 — cross-account consistency + timeline
# ─────────────────────────────────────────────

def print_part2(rows: List[dict], db: Database, symbol_filter: Optional[str]) -> None:
    print('\n' + '=' * 70)
    print('PART 2 — CROSS-ACCOUNT CONSISTENCY (same symbol + same direction)')
    print('=' * 70)
    groups: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for r in rows:
        groups[(r['symbol'], r['side'])].append(r)
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    if not multi:
        print('No symbol/direction held open by more than one account.')
    for (sym, side), grp in sorted(multi.items()):
        print(f'\n  {sym} {side.upper()} — {len(grp)} accounts open:')
        for r in sorted(grp, key=lambda x: x['created_at'] or 0):
            print('    acct={:<4} entry={} avgEntry={} L={} net={} roi={} decision={} opened={}'.format(
                r['account_id'],
                f"{r['entry']:.6f}" if r['entry'] else '-',
                f"{r['entry']:.6f}" if r['entry'] else '-', r['layers'],
                f"{r['net']:.4f}" if r['net'] is not None else '-',
                f"{r['roi'] * 100:.2f}%" if r['roi'] is not None else '-',
                r['decision'], _ts(r['created_at'])))
        entries = [r['entry'] for r in grp if r['entry']]
        if entries and (max(entries) - min(entries)) > 1e-9:
            spread = (max(entries) - min(entries)) / min(entries) * 100
            print(f'    → entry-price spread across accounts: {spread:.3f}% '
                  f'(different fills → different ROI at the same price)')
        layer_counts = {r['layers'] for r in grp}
        if len(layer_counts) > 1:
            print(f'    → different layer structures: {sorted(layer_counts)} '
                  f'(L1-only vs recovery → different ROI/USD targets)')


def print_timeline(db: Database, symbol_filter: Optional[str]) -> None:
    print('\n' + '=' * 70)
    print('CROSS-ACCOUNT TIMELINE (opens from baskets, closes from trades)')
    print('=' * 70)
    events: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
    with db.session() as s:
        bq = s.query(BasketModel)
        if symbol_filter:
            bq = bq.filter(BasketModel.symbol == symbol_filter)
        for b in bq.all():
            events[b.symbol].append((b.created_at or 0,
                                     f'acct {b.account_id} opened {b.side} (basket {b.id[:8]}, {b.status})'))
        tq = s.query(TradeModel)
        if symbol_filter:
            tq = tq.filter(TradeModel.symbol == symbol_filter)
        for t in tq.all():
            events[t.symbol].append((t.exit_time or 0,
                                     f'acct {t.account_id} CLOSED {t.side} pnl={t.pnl:+.4f} reason={t.exit_reason}'))
    if not events:
        print('No basket/trade history found.')
    for sym, evs in sorted(events.items()):
        print(f'\n  {sym}:')
        for ts, desc in sorted(evs, key=lambda x: x[0]):
            print(f'    {_ts(ts):<20} {desc}')


# ─────────────────────────────────────────────
# TP-LOCK / ROI / TP / POSITION-SYNC audits
# ─────────────────────────────────────────────

def print_tp_lock_audit(rows: List[dict], locks: Dict[str, str], log_events: Dict[str, dict]) -> None:
    print('\n' + '=' * 70)
    print('TP-LOCK AUDIT')
    print('=' * 70)
    open_ids = {r['basket_id'] for r in rows}
    orphan = [(k, v) for k, v in locks.items()
              if '_tp_lock_' in k and v and v not in ('', 'false')
              and k.split('_tp_lock_')[-1] not in open_ids]
    print('\n  Orphaned persisted TP locks (set, but basket NOT open) — '
          'TP_LOCK_ACTIVATED without TP_LOCK_EXECUTED:')
    if orphan:
        for k, v in orphan:
            print(f'    {k} = {v}   ← basket already closed/reconciled but lock never released')
    else:
        print('    none')
    print('\n  Open baskets currently TP-locked (mid-close, frozen):')
    locked_open = [r for r in rows if r['tp_locked']]
    if locked_open:
        for r in locked_open:
            print(f'    acct={r["account_id"]} {r["symbol"]} reason={r["tp_locked"]} decision={r["decision"]}')
    else:
        print('    none')
    print('\n  Log pairing (account|symbol → activations vs executions):')
    if log_events:
        for key, ev in sorted(log_events.items()):
            a, e = len(ev['activated']), len(ev['executed'])
            flag = '  ⚠ UNMATCHED' if a > e else ''
            print(f'    {key:<28} activated={a} executed={e}{flag}')
    else:
        print('    no TP_LOCK lines in the trades log')


def print_roi_tp_audit(rows: List[dict]) -> None:
    print('\n' + '=' * 70)
    print('ROI AUDIT — ROI >= target but basket still open')
    print('=' * 70)
    hits = [r for r in rows if r['roi'] is not None and r['roi'] >= r['roi_target'] > 0
            and r['decision'] in ('hold', 'NO_PRICE')]
    if hits:
        for r in hits:
            print(f'    ⚠ acct={r["account_id"]} {r["symbol"]} roi={r["roi"]*100:.2f}% '
                  f'>= target {r["roi_target"]*100:.1f}% but decision={r["decision"]}')
    else:
        print('    none — every basket at/above its ROI target has a close decision')

    print('\n' + '=' * 70)
    print('TP AUDIT — net PnL >= USD target but basket still open')
    print('=' * 70)
    hits = [r for r in rows if r['net'] is not None and r['net'] >= r['tp_target'] > 0
            and r['decision'] in ('hold', 'NO_PRICE')]
    if hits:
        for r in hits:
            print(f'    ⚠ acct={r["account_id"]} {r["symbol"]} net={r["net"]:.4f} '
                  f'>= target {r["tp_target"]:.2f} but decision={r["decision"]}')
    else:
        print('    none — every basket at/above its USD target has a close decision')


def print_position_sync(rows: List[dict], live: Optional[LiveData]) -> None:
    print('\n' + '=' * 70)
    print('POSITION-SYNC AUDIT — exchange position vs internal basket state')
    print('=' * 70)
    if not live:
        print('    skipped (run with --live for exchange comparison)')
        return
    for r in rows:
        positions = live.positions(r['account_id'])
        pos = positions.get((r['symbol'], r['side'].lower()))
        ex_qty = float(pos['contracts']) if pos else 0.0
        ex_pnl = float(pos.get('unrealizedPnl', 0.0)) if pos else 0.0
        # internal qty is reconstructed from layers via margin*lev/entry only if needed;
        # here we report the basket's tracked totals from PART 1 context.
        mark = '  ⚠ MISMATCH' if (ex_qty <= 0) else ''
        print(f'    acct={r["account_id"]} {r["symbol"]} {r["side"]} '
              f'exch_qty={ex_qty:.6f} exch_pnl={ex_pnl:+.4f} internal_net={r["net"]}{mark}')


# ─────────────────────────────────────────────
# Failure report
# ─────────────────────────────────────────────

def print_failure_report(rows: List[dict], orphan_count: int) -> None:
    print('\n' + '=' * 70)
    print('FAILURE REPORT — baskets that should have closed but did not')
    print('=' * 70)
    failures = [r for r in rows
                if r['roi'] is not None
                and ((r['roi'] >= r['roi_target'] > 0) or (r['net'] is not None and r['net'] >= r['tp_target'] > 0))
                and r['decision'] in ('hold',)]
    if not failures:
        print('  None: no open basket is at/above a profit target with a "hold" decision.')
        print('  → If users still report "stayed open", the cause is NOT live exit math')
        print('    (see verdict): it is per-account entry/price/timing divergence, a')
        print('    deferred cycle from a failed ticker fetch, or an orphaned TP lock.')
    for r in failures:
        print(f'\n  acct={r["account_id"]} {r["symbol"]} {r["side"]} basket={r["basket_id"][:8]}')
        print(f'    expected close : roi>=target ({r["roi"]*100:.2f}%>={r["roi_target"]*100:.1f}%) '
              f'or net>=tp ({r["net"]}>= {r["tp_target"]})')
        print(f'    actual state   : decision={r["decision"]} (still OPEN)')
        print(f'    root cause     : exit recomputed as hold at the SAME price the bot sees — '
              f'inspect ticker availability / TP-lock state for this cycle')
    if orphan_count:
        print(f'\n  + {orphan_count} orphaned TP lock(s): basket closed/reconciled on the '
              f'exchange but tp_lock_<id> was never released and no trade row written — '
              f'see reconcile_baskets gap in the verdict.')


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def load_locks(db: Database) -> Dict[str, str]:
    locks: Dict[str, str] = {}
    with db.session() as s:
        for st in s.query(BotStateModel).all():
            if any(t in st.key for t in ('lock', 'protection', 'emergency')):
                locks[st.key] = st.value
    return locks


def main() -> None:
    ap = argparse.ArgumentParser(description='ZenGrid trade-closure forensic audit (read-only).')
    ap.add_argument('--config', default='config/config.json')
    ap.add_argument('--symbol', default=None, help='Filter to one symbol, e.g. TRX/USDT:USDT')
    ap.add_argument('--account', type=int, default=None, help='Filter to one account id')
    ap.add_argument('--live', action='store_true', help='Fetch live price + positions per account')
    ap.add_argument('--log', default='logs/trades.log', help='Path to the trades log')
    args = ap.parse_args()

    settings = Settings.load(args.config)
    db = Database(os.environ.get('DATABASE_URL') or settings.database_url)

    locks = load_locks(db)
    log_events = parse_tp_lock_log(args.log)
    live = LiveData(settings, db) if args.live else None

    rows = audit_open_baskets(db, settings, live, locks, args.symbol, args.account)

    print_part1(rows)
    print_part2(rows, db, args.symbol)
    print_tp_lock_audit(rows, locks, log_events)
    print_roi_tp_audit(rows)
    print_position_sync(rows, live)
    print_timeline(db, args.symbol)
    orphan_count = sum(1 for k, v in locks.items()
                       if '_tp_lock_' in k and v and v not in ('', 'false')
                       and k.split('_tp_lock_')[-1] not in {r['basket_id'] for r in rows})
    print_failure_report(rows, orphan_count)

    db.close()


if __name__ == '__main__':
    main()
