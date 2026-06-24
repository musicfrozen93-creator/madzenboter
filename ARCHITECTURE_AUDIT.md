# ZenGrid Futures Core — Single-Entry Scalping Architecture

Single-entry scalping core for Binance USDT-M Futures. **Fixed 100-symbol**
USDT-M perpetual universe, **15m** timeframe, **10×** leverage. One position per
symbol. Survival-first. **No recovery, no Layer 2, no averaging down, no
martingale, no grid expansion.**

> **Replaces** the previous Dark-Venus basket-recovery model. Recovery layers,
> basket averaging, ROI exits, exposure caps, and the correlation second-symbol
> rule were removed. The `baskets`/`recovery_layers` tables and `Basket`/
> `RecoveryLayer` DTOs are retained (single entry = one basket, one layer) so the
> database schema shared with the web platform is unchanged — no migration.

---

## Final parameters

| Parameter | Value |
|-----------|-------|
| Universe | fixed 100 USDT-M perps (scan only these; BTC filter-only, ETH excluded) |
| Timeframe | 15m |
| Leverage | 10× (admin override 8×–10×, hard cap 10×) |
| Tier 1 ($20–39.99) | margin $0.8, 8 positions, daily +$2/−$3, floor $15 |
| Tier 2 ($40+) | margin $1.5, 10 positions, daily +$3.5/−$4, floor $30 |
| Take-profit | 20% of margin (net) → T1 $0.16 / T2 $0.30 |
| Stop-loss | 12% of margin (net) → T1 $0.096 / T2 $0.18 |
| Portfolio profit lock | dynamic trail `max(floor, peak×band%)` — arm/floor T1 $0.50/$0.35 · T2 $0.80/$0.50; bands 70→85% |
| Symbol cooldown | 30 min (symbol-specific) after a close |
| ATR entry band | 0.30% ≤ ATR/price ≤ 1.20% |
| Min signal score | 1 (of 0–4) |
| Taker fee model | 0.05% (round-trip 0.10% of notional) |

---

## Entry Logic

`signals/signal_engine.py` (shared public market data, fanned out to every
eligible account):

1. Symbol must be one of the fixed 100, else skip.
2. Fetch 15m OHLCV; warm up RSI(14), ATR(14), Bollinger(20, 2σ).
3. **Pre-trade filters** (skip with a logged reason if any trips): spread too
   high, ATR explosion (ATR > 2.5× avg), news/oversized candle (body > 2.5×
   ATR), volume spike (> 3× avg), **ATR feasibility band** (0.30%–1.20%).
4. **Mean-reversion conditions**: LONG `RSI<30` + lower-BB touch; SHORT `RSI>70`
   + upper-BB touch.
5. **BTC 15m trend filter** gates direction.
6. **Signal-strength score (0–4)** computed; the position gate requires
   `score ≥ min_signal_score`.

Account-level entry gating in `grid/position_manager.open_position`, in order:
bot-control → supported-symbol → tier resolution → **[1] lock status → [2] daily
profit → [3] daily loss** (`can_take_new_entry`) → **[4] cooldown** → one-per-
symbol → max active symbols → max positions → **signal-quality score** →
exchange-safety sizing → execute (sized at a fresh execution-time price,
partial-fill aware).

## Exit Logic

`grid/take_profit.py::evaluate_exit` + `grid/position_manager.manage_baskets`,
priority order:

- **P0 — Account death protection:** equity < tier floor → `protection_lock` + close all (permanent).
- **P1 — Daily loss limit:** realised + unrealised ≤ −tier limit → close all + lock.
- **P1.5 — Portfolio trailing profit lock (dynamic):** arms when total open
  unrealised PnL ≥ tier trigger ($0.50 T1 / $0.80 T2), stores the running peak,
  and flattens ALL positions (`portfolio_profit_lock`) the moment current profit
  drops below `protected = max(floor, peak × band%)`. The protection % ratchets
  up with the peak (T1 70/75/80/85% at peaks ≥0.50/1.00/1.50/2.00; T2 same % at
  peaks ≥0.80/2.00/3.00/4.00) and the protected level never falls. Per-account,
  resets when flat / on new day, independent of the daily profit lock.
- **P2 — Position exit:** net ≥ `tp_margin_pct × margin` (20%) → `tp` (TP-locked close); net ≤ −`sl_margin_pct × margin` (12%) → `sl`.

Daily profit target latches the new-entry lock (no closing). Both targets use
**net** PnL (gross − round-trip taker fees). There is **no recovery layer step**.

**Immediate TP execution:** when the TP condition becomes true the bot, in the
**same management cycle**, logs `TP_DETECTED`, activates + persists the TP lock,
and submits the close (`TP_CLOSE_SENT` → `TP_CLOSE_CONFIRMED`) — no wait for a
later cycle, no TP re-evaluation. A hit target can never keep running.

## TP Lock (exit-execution guarantee)

When the take-profit fires, the exit is **committed and frozen**: `tp_lock_<id>`
is persisted (account-scoped) and `manage_baskets` stops re-evaluating, only
retrying closure until the position is flat and exchange-confirmed (then
`TP_LOCK_EXECUTED`). The lock survives restart/crash, so a reached target can
never be left open by a post-target reversal. Orphaned locks (position already
closed/reconciled) are cleaned at startup.

## Position Sizing

`risk/position_sizer.build_order`: `quantity = (margin × leverage) / price`,
floored to the lot step, then validated against min notional / min quantity /
step / precision. Margin is the tier's fixed `margin_per_trade` — never
balance-scaled. Tier-1 notional $8 and Tier-2 notional $15 both clear the
5-USDT floor with headroom, so order acceptance is reliable for the curated
5-USDT-min-notional universe.

## Risk Management

Layered, survival-first: pre-trade filters → BTC gate → ATR band → signal-quality
score → fixed tier sizing → daily profit/loss locks → permanent death
protection. Leverage fixed (10× default, 8–10× admin, 10× hard cap), never
dynamic. Partial fills tracked as actual qty/margin. Exchange-safety validation
before every order.

## Account Isolation

Every account has its own `RiskManager` and an `AccountDatabaseWrapper` that
prefixes all state keys `account_<id>_…` and forces account-scoped trade/position
queries. Daily counters, locks, cooldowns, position state, and protection locks
are fully independent. A per-account limit never affects another account and
never globally stops the bot. All locks persist across restart/crash.

## Reconciliation

`reconcile_baskets` finalizes any DB position whose live exchange position has
vanished (closed externally, lost fill, liquidation): it persists a trade record
(reason = the committed TP-lock reason if held, else `reconciled`), releases any
orphaned TP lock, and starts the cooldown.

---

## Trade-closure reasons

| Reason | Trigger | Scope |
|--------|---------|-------|
| `tp` | net ≥ tp_margin_pct × margin (20%) | this position (TP-locked, immediate) |
| `sl` | net ≤ −sl_margin_pct × margin (12%) | this position |
| `portfolio_profit_lock` | armed aggregate fell below `max(floor, peak×band%)` | ALL positions, lock resets when flat |
| `daily_loss_limit` | realised + unrealised ≤ −tier limit | ALL positions, lock to UTC reset |
| `protection_lock` | equity < tier floor | ALL positions, permanent lock |
| `force_close_all` | admin force-close | ALL positions |
| `reconciled` | exchange position vanished | this position |

---

## Expectations (structural, not backtested)

- **Trades/day:** ~30–60 (Tier 1, 8 slots) / ~40–80 (Tier 2, 10 slots), gated by
  signals + the 15-min cooldown across the 100-symbol universe.
- **Trade duration:** median ~1–1.5 h (range 30 min – 3 h); winners faster.
- **Break-even win rate:** ≈ 35% (R/R ≈ 2.08:1, net of fees). Profitable above
  that; mean-reversion entries target ~40–50%.
- **Daily drawdown:** bounded by the tier daily loss limit, then the death floor.

## Remaining weaknesses

1. **Fixed-% SL across 100 different-volatility symbols.** The ATR feasibility
   band keeps the 12% stop outside noise, but a single % stop still behaves
   differently per symbol; a per-symbol ATR-based stop would be more uniform.
2. **Correlation cluster risk.** Concurrent positions are not independent; a
   BTC-led flush can stop several at once. The daily loss limit + death floor
   bound this, and the diversified 100-symbol universe lowers it vs the old
   correlated-alt list.
3. **Funding fees unmodeled** (only taker fees are netted). Sub-hour scalps
   mostly avoid the 8-hour settlements; long holds across a settlement are not.
4. **Death protection uses real equity** (wallet + floating), so deposits move an
   account away from its floor — intentional (real survival value) but differs
   from the deposit-immune daily PnL.
5. **Universe tail (symbols ~80–100)** is the thinnest liquidity band; the
   startup validator drops any that go inactive or exceed the Tier-1 notional,
   but does not add replacements (the universe stays fixed).
