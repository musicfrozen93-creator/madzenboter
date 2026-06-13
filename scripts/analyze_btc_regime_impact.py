"""
Backtest the BTC Regime Participation Filter (Change #3) against history.

Answers, for the last N days (default 7):
    • total signals generated
    • signals allowed
    • signals blocked
    • blocked longs
    • blocked shorts
    • estimated TRADE-count impact (blocked actual basket-opens)

Method
------
The `signals` table records what the SignalEngine generated (symbol, side,
created_at) but NOT the BTC regime at that moment. So we reconstruct it:

1. Read signals (and, separately, successful opens from execution_logs) for
   the window from the production DB.
2. Fetch BTC 1h candles covering the window + a 200-bar EMA warm-up, keylessly
   from Binance via ccxt (public data, no API keys).
3. Pre-compute EMA200 + ADX over that series (causal, backward-looking).
4. For each signal timestamp, take the last closed 1h bar at-or-before it and
   classify the BTC regime using the EXACT thresholds the live filter uses.
5. Apply the live gate (BTCRegimeFilter.allows) to decide allow/block.

Signal-level counts overcount trades (the same extreme RSI re-emits a signal
every loop, but the one-basket-per-symbol rule opens only once). The
trade-level estimate therefore replays the gate over ACTUAL successful opens
(execution_logs action='open', status='success'), which is the realistic
"how many trades would not have happened" number.

Usage
-----
    python -m scripts.analyze_btc_regime_impact --days 7
    python -m scripts.analyze_btc_regime_impact --days 7 --db-url postgresql://...
    python -m scripts.analyze_btc_regime_impact --self-test   # offline correctness check
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# Ensure project root on path when run as a file.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings
from signals.btc_regime import BTCRegime, BTCRegimeFilter
from signals.indicators import compute_adx, compute_ema


# ───────────────────────────────────────────
# Regime replay (mirrors BTCRegimeFilter._classify, vectorised over history)
# ───────────────────────────────────────────

def build_regime_series(btc_1h: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Return a frame indexed by candle close-time(ms) with a 'regime' column.

    Uses the identical inputs/thresholds as BTCRegimeFilter._classify:
        strong = ADX > adx_trend_threshold
        UP   = strong and close > EMA200
        DOWN = strong and close < EMA200
        else SIDEWAYS  (UNKNOWN only where EMA/ADX are NaN — warm-up)
    """
    ema = compute_ema(btc_1h['close'], period=settings.ema_period)
    adx = compute_adx(btc_1h['high'], btc_1h['low'], btc_1h['close'],
                      period=settings.adx_period)
    thr = settings.adx_trend_threshold

    regimes = []
    for close, e, a in zip(btc_1h['close'], ema, adx):
        if pd.isna(e) or pd.isna(a):
            regimes.append(BTCRegime.UNKNOWN)
        elif a > thr and close > e:
            regimes.append(BTCRegime.UP_IMPULSE)
        elif a > thr and close < e:
            regimes.append(BTCRegime.DOWN_IMPULSE)
        else:
            regimes.append(BTCRegime.SIDEWAYS)

    out = pd.DataFrame({'ts': btc_1h['timestamp'].astype('int64'), 'regime': regimes})
    return out.sort_values('ts').reset_index(drop=True)


def regime_at(regime_series: pd.DataFrame, ts_ms: int) -> BTCRegime:
    """BTC regime from the last 1h bar at-or-before ts_ms (UNKNOWN if none)."""
    prior = regime_series[regime_series['ts'] <= ts_ms]
    if prior.empty:
        return BTCRegime.UNKNOWN
    return prior.iloc[-1]['regime']


def tally(rows, regime_series, gate: BTCRegimeFilter) -> dict:
    """rows: iterable of (side, ts_ms). Returns count breakdown."""
    res = {'total': 0, 'allowed': 0, 'blocked': 0,
           'blocked_long': 0, 'blocked_short': 0,
           'by_regime': {}}
    for side, ts_ms in rows:
        res['total'] += 1
        reg = regime_at(regime_series, ts_ms)
        gate._regime = reg  # drive the REAL gate with the historical regime
        allowed, _ = gate.allows(side)
        res['by_regime'][reg.value] = res['by_regime'].get(reg.value, 0) + 1
        if allowed:
            res['allowed'] += 1
        else:
            res['blocked'] += 1
            if side == 'long':
                res['blocked_long'] += 1
            elif side == 'short':
                res['blocked_short'] += 1
    return res


# ───────────────────────────────────────────
# Data sources
# ───────────────────────────────────────────

def fetch_btc_1h(symbol: str, since_ms: int) -> pd.DataFrame:
    import ccxt
    ex = ccxt.binance({'options': {'defaultType': 'future'}, 'enableRateLimit': True})
    ex.load_markets()
    all_rows, cursor = [], since_ms
    while True:
        batch = ex.fetch_ohlcv(symbol, '1h', since=cursor, limit=1000)
        if not batch:
            break
        all_rows += batch
        cursor = batch[-1][0] + 3_600_000
        if len(batch) < 1000 or cursor > ex.milliseconds():
            break
        time.sleep(ex.rateLimit / 1000)
    df = pd.DataFrame(all_rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    return df.drop_duplicates('timestamp').reset_index(drop=True)


def read_rows(db_url: str, days: int):
    """Return (signal_rows, open_rows), each a list of (side, ts_ms)."""
    from sqlalchemy import create_engine, text
    eng = create_engine(db_url, connect_args={'connect_timeout': 5})
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with eng.connect() as c:
        sigs = c.execute(text(
            "SELECT side, created_at FROM signals WHERE created_at >= :c"
        ), {'c': cutoff}).fetchall()
        opens = c.execute(text(
            "SELECT side, executed_at FROM execution_logs "
            "WHERE action='open' AND status='success' AND executed_at >= :c"
        ), {'c': cutoff}).fetchall()
    to_ms = lambda dt: int(dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp() * 1000)
    return ([(s, to_ms(t)) for s, t in sigs],
            [(s, to_ms(t)) for s, t in opens])


# ───────────────────────────────────────────
# Report
# ───────────────────────────────────────────

def print_report(days, sig, trade):
    line = '─' * 58
    print(f'\n{line}\nBTC REGIME FILTER — RETROSPECTIVE IMPACT (last {days} days)\n{line}')
    print('SIGNAL-LEVEL (every generated signal, pre-fan-out):')
    print(f'  total signals generated : {sig["total"]}')
    print(f'  signals allowed         : {sig["allowed"]}')
    print(f'  signals blocked         : {sig["blocked"]}')
    print(f'    blocked longs         : {sig["blocked_long"]}')
    print(f'    blocked shorts        : {sig["blocked_short"]}')
    pct = (100 * sig['blocked'] / sig['total']) if sig['total'] else 0
    print(f'  blocked %               : {pct:.1f}%')
    print(f'  signals by BTC regime   : {sig["by_regime"]}')
    print(f'\nTRADE-LEVEL (actual successful basket opens):')
    print(f'  total opens             : {trade["total"]}')
    print(f'  opens that remain        : {trade["allowed"]}')
    print(f'  opens blocked (est.)    : {trade["blocked"]}')
    print(f'    blocked longs         : {trade["blocked_long"]}')
    print(f'    blocked shorts        : {trade["blocked_short"]}')
    tpct = (100 * trade['blocked'] / trade['total']) if trade['total'] else 0
    print(f'  trade-count impact      : -{trade["blocked"]} opens (-{tpct:.1f}%)')
    print(line)
    print('Note: signal-level counts overcount trades (repeated signals per\n'
          'extreme; one-basket-per-symbol opens once). The trade-level row is\n'
          'the realistic frequency impact.\n')


# ───────────────────────────────────────────
# Self-test (offline correctness proof)
# ───────────────────────────────────────────

def self_test():
    import numpy as np
    # EMA200 needs a 200-bar warm-up, so the trend leg must extend well past
    # bar 200 before we sample it. Strong uptrend (bars 0..399) → long flat
    # range (bars 400..699) so ADX decays below the trend threshold.
    n_up, n_flat = 400, 300
    up = np.linspace(100, 500, n_up)
    flat = 500 + np.sin(np.arange(n_flat) / 3.0) * 0.3
    closes = np.concatenate([up, flat])
    n = len(closes)
    base_ms = 1_700_000_000_000
    btc = pd.DataFrame({
        'timestamp': base_ms + np.arange(n) * 3_600_000,
        'open': closes, 'high': closes * 1.001, 'low': closes * 0.999,
        'close': closes, 'volume': np.ones(n),
    })
    s = Settings()
    rs = build_regime_series(btc, s)
    gate = BTCRegimeFilter(exchange_client=None, settings=s)

    # A short deep in the (warmed-up) uptrend must be BLOCKED; both sides in the
    # settled flat range must be ALLOWED.
    up_ts = int(btc['timestamp'].iloc[n_up - 10])
    flat_ts = int(btc['timestamp'].iloc[-3])
    rows = [('short', up_ts), ('long', up_ts), ('short', flat_ts), ('long', flat_ts)]
    r = tally(rows, rs, gate)
    assert regime_at(rs, up_ts) == BTCRegime.UP_IMPULSE, regime_at(rs, up_ts)
    assert regime_at(rs, flat_ts) == BTCRegime.SIDEWAYS, regime_at(rs, flat_ts)
    assert r['total'] == 4 and r['blocked'] == 1, r
    assert r['blocked_short'] == 1 and r['blocked_long'] == 0, r
    assert r['allowed'] == 3, r
    print('SELF-TEST PASSED — replay classifies UP_IMPULSE/SIDEWAYS and the '
          'real gate blocks exactly the counter-trend short.')
    print('  tally:', {k: v for k, v in r.items()})


# ───────────────────────────────────────────
# Main
# ───────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--db-url', default=None, help='defaults to config.json database_url')
    ap.add_argument('--config', default='config/config.json')
    ap.add_argument('--self-test', action='store_true', help='offline correctness check, no DB/network')
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    settings = Settings.load(args.config)
    db_url = args.db_url or settings.database_url

    print(f'Reading signals/opens for the last {args.days} days from DB...')
    sig_rows, open_rows = read_rows(db_url, args.days)
    print(f'  signals: {len(sig_rows)}  |  successful opens: {len(open_rows)}')
    if not sig_rows and not open_rows:
        print('No history in window — nothing to analyse.')
        return

    earliest_ms = min((ts for _, ts in (sig_rows + open_rows)), default=None)
    warmup_ms = (settings.ema_period + 50) * 3_600_000  # EMA200 warm-up
    since = earliest_ms - warmup_ms
    print(f'Fetching BTC 1h candles for {settings.btc_regime_symbol} since '
          f'{datetime.fromtimestamp(since/1000, timezone.utc):%Y-%m-%d %H:%M}Z ...')
    btc = fetch_btc_1h(settings.btc_regime_symbol, since)
    print(f'  fetched {len(btc)} BTC 1h candles')

    regime_series = build_regime_series(btc, settings)
    gate = BTCRegimeFilter(exchange_client=None, settings=settings)

    sig = tally(sig_rows, regime_series, gate)
    trade = tally(open_rows, regime_series, gate)
    print_report(args.days, sig, trade)


if __name__ == '__main__':
    main()
