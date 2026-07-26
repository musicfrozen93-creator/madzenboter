# Zentry AI Crypto Trading Bot — Complete Technical Audit

> **Purpose:** Reverse-engineered, source-verified documentation of how the bot
> actually operates *today*. Nothing here is a recommendation or redesign except
> where explicitly labelled in **Section 13 — Known Limitations**. Every claim
> below was read directly from the code, not from docstrings (several docstrings
> are stale — see Section 13).

**Codebase:** `madzenboter-main/` — ~7,000 LOC Python. Strategy internally named
**"ZenGrid / Dark-Venus basket recovery"**. Exchange: **Binance USDT-M Futures**
via `ccxt`. Persistence: **PostgreSQL** via SQLAlchemy.

---

## SECTION 1 — SYSTEM ARCHITECTURE

### 1.1 Folder structure

| Folder | Responsibility |
|---|---|
| `config/` | Central `Settings` dataclass + `config.json` — single source of truth for every strategy parameter. |
| `core/` | `engine.py` (main loop), `database.py` (repository), `models.py` (ORM tables), `dto.py` (in-memory dataclasses). |
| `signals/` | `signal_engine.py` (entry signals), `indicators.py` (RSI/ATR/BB/EMA), `btc_regime.py` (BTC trend filter). |
| `grid/` | `position_manager.py` (basket lifecycle), `take_profit.py` (exit math), `recovery.py` (Layer-2 trigger). |
| `risk/` | `risk_manager.py` (daily limits + death protection), `position_sizer.py` (margin→quantity). |
| `execution/` | `executor.py` — multi-account fan-out + per-account component construction/caching. |
| `exchange/` | `client.py` (ccxt wrapper), `utils.py` (rounding, min-notional). |
| `accounts/` | `manager.py` (account CRUD), `encryption.py` (Fernet), `models.py` (Pydantic-ish account DTOs). |
| `control/` | `bot_control.py` — thread-safe control-plane singleton (start/stop/emergency/force-close). |
| `services/` | `sync.py` — background daemon syncing balances/positions/risk metrics. |
| `admin/` | FastAPI REST API (accounts, positions, trades, bot control). |
| `alembic/` | DB migrations. `scripts/` | maintenance (`closure_audit.py`, key generation, SQLite→PG migration). |
| `tests/` | pytest suite (isolation, sizing, TP, recovery, risk limits, BTC filter, exit hardening). |

### 1.2 Main components & how they communicate

```
main.py
  └─ Settings.load(config.json)          # merges env vars over JSON
  └─ TradingEngine(settings)
        ├─ Database (Postgres pool)       # shared, single instance
        ├─ ExchangeClient.for_market_data # KEYLESS public client (signals only)
        ├─ SignalEngine(public client)
        ├─ BotControl()                    # control-plane singleton
        ├─ EncryptionService(master key)
        ├─ AccountManager(db, enc)
        ├─ SignalExecutor(db, acct_mgr, enc, bot_control)   # the trading brain
        └─ SyncService(db, acct_mgr, enc) # background daemon thread
```

- The **engine** holds *no* trading balance, *no* global risk state, and *no*
  global shutdown. It only refreshes shared market data and delegates.
- All *trading* (orders, balances, risk, TP/SL) happens **per account** inside
  `SignalExecutor`, which builds an isolated stack per account:
  `ExchangeClient (with keys) + RiskManager + PositionManager + PositionSizer +
  RecoverySystem + TakeProfitManager`, all bound to an
  **`AccountDatabaseWrapper`** that namespaces every state key and query.

### 1.3 Execution flow (the main loop — `core/engine.py:_run_loop`)

Every `loop_interval_seconds` (**10 s**):

1. **EXITS FIRST** — if `manage_existing_positions` is on,
   `SignalExecutor.manage_all_accounts()` runs TP/SL/recovery/protection for
   every managed account (concurrently). This is *always first* so exits are
   never starved behind signal generation.
2. Set scanner-running flag.
3. **NEW ENTRIES** (throttled to `signal_eval_interval_seconds` = **30 s**) —
   `_evaluate_signals()` generates one signal per supported symbol and fans each
   out to all eligible accounts.
4. **Status log** every 5 min.
5. Sleep the remainder of the 10 s interval.

**Key design point:** exit management runs on the tight 10 s cadence; new-entry
signal generation (the slow phase) runs at most every 30 s.

### 1.4 Threading model

- **Main thread:** the trading loop.
- **`admin-api` thread** (daemon, only if `--api`): uvicorn FastAPI server.
- **`sync-service` thread** (daemon): balance/position/risk-metric sync every 60 s.
- **Per-phase `ThreadPoolExecutor`** (`max_workers=50`): inside
  `manage_all_accounts()` and `execute_signal()`, each *account* is processed on
  its own worker thread. A failure in one account never affects others.
- Idempotent-close guard: `PositionManager._closing_lock` (a `threading.Lock`) +
  `_closing` set prevent double-closing a basket across threads.
- `BotControl` uses a `threading.Lock` for mutations; hot-path reads are lock-free.

### 1.5 Multi-account architecture & isolation

- **Accounts live in the DB** (`accounts` table). Each has Fernet-encrypted
  Binance API keys, `is_active`, `use_testnet`, and a `leverage_override`.
- The bot has **no master/fallback trading account** — the container's own
  exchange client is keyless/public and *structurally cannot place orders*
  (`place_market_order` raises `PermissionError` without credentials).
- **Isolation mechanism — `AccountDatabaseWrapper`** (`execution/executor.py:35`):
  - `get_state`/`set_state` are prefixed `account_<id>_<key>`, so every lock,
    cooldown, TP-lock, HWM, and daily-limit latch is per-account.
  - `load_active_baskets`, `save_trade`, `get_today_trades` are forced to the
    account's `id`.
- **Component cache** (`_component_cache`): the built stack per account is cached
  and reused across loops; rebuilt only when a `_component_fingerprint` changes
  (keys, testnet, risk_pct, leverage_override, tp/sl settings). This avoids a
  `load_markets()` network round-trip per account per coin. Balance and risk
  state are always re-read at use-time so cached components never go stale.

### 1.6 Data flow

```
Binance (public OHLCV/ticker) ─► SignalEngine ─► Signal DTO
        └► saved to `signals` table (audit)
Signal ─► SignalExecutor.execute_signal
        └► eligibility gate (DB: user active + subscription active + account active)
        └► per eligible account: PositionManager.open_position
              └► ExchangeClient.place_market_order (per-account keys)
              └► Basket + RecoveryLayer persisted (baskets / recovery_layers tables)
              └► execution_logs row
Loop ─► manage_all_accounts ─► per account: reconcile ─► manage_baskets
        └► TakeProfit / Recovery / RiskManager decisions
        └► close_basket ─► `trades` table + cooldown state
SyncService (background) ─► positions / risk_metrics / accounts.cached_balance
```

---

## SECTION 2 — ENTRY LOGIC

A trade becomes valid in **two stages**: (A) the **SignalEngine** must emit a
signal (market-data gates, same for all accounts), then (B) the
**PositionManager.open_position** must approve it (per-account risk/structural
gates). Both must pass.

### 2.1 Stage A — SignalEngine (`signals/signal_engine.py:generate_signal`)

Runs per supported symbol on the **15m** timeframe, `candle_limit=300` bars.

**Core mean-reversion conditions (both parts required):**
- **LONG:** `RSI(14) < 30` **AND** candle **low ≤ lower Bollinger band**.
- **SHORT:** `RSI(14) > 70` **AND** candle **high ≥ upper Bollinger band**.

**Order of checks inside generate_signal:**

1. **Supported-symbol** guard → skip `unsupported_symbol`.
2. **Sufficient data** — need `max(bb,rsi,atr)+lookback+2` bars → skip `insufficient_data`.
3. **Indicator warm-up / NaN** guards → skip.
4. **Spread** read from ticker; `last` price refreshed from ticker.
5. **Pre-trade risk-rule skip filters** (Section 2.2). Any trip → skip.
6. **Mean-reversion condition** (RSI + BB). Neither side → skip `no_setup`.
7. **BTC trend filter** (Section 2.3). Blocks the side → skip `btc_filter`.
8. Compute **strength (0.1–1.0)** and **strength_score (0–4)** (Section 2.6).
9. Emit `Signal` and log `SIGNAL_FOUND`.

### 2.2 The filters — purpose / calculation / threshold / why

| Filter | Calculation | Threshold (config) | Purpose |
|---|---|---|---|
| **RSI** | Wilder's RSI, period 14 | `< 30` long / `> 70` short (`rsi_oversold`/`rsi_overbought`) | Identify oversold/overbought extremes — the mean-reversion trigger. |
| **Bollinger Bands** | SMA(20) ± 2·σ (`ddof=0`) | candle low ≤ lower / high ≥ upper (`bb_period=20`, `bb_std=2.0`) | Confirm price has stretched to a statistical band edge, not just an RSI reading. |
| **BTC trend filter** | BTC 15m EMA50 vs EMA200 + price vs EMA200 | `btc_filter_enabled=true` | Global direction gate — don't fight the market's dominant trend (Section 2.3). |
| **ATR explosion** | current ATR vs mean ATR over lookback | `> 2.5×` avg (`atr_explosion_multiplier`) | Skip abnormally volatile conditions where mean-reversion breaks down. |
| **Spread filter** | `spread / price` from ticker bid/ask | `> 0.10%` (`max_spread_pct=0.0010`) | Avoid illiquid/wide-spread markets where fills are costly. |
| **Liquidity/Volume (skip)** | last volume vs mean volume over lookback | `> 3×` avg (`volume_spike_multiplier`) | Skip volume-spike candles (often news-driven, unreliable reversion). |
| **News candle** | candle body `|close-open|` vs ATR | `> 2.5×` ATR (`news_candle_atr_multiplier`) | Skip large single-candle moves (event risk). |
| **Cooldown** | time since last close of this symbol | `900 s` (`symbol_cooldown_seconds`) | Enforced in Stage B — prevents immediate re-entry (Section 9). |

*Lookback for ATR/volume averages:* `risk_filter_lookback = 30` bars.

### 2.3 BTC trend filter (`signals/btc_regime.py`)

Pure function over BTC 15m candles. Regime cached `btc_regime_cache_seconds`=**300 s**.

- **BULLISH:** price > EMA200 **and** EMA50 > EMA200 → **allow LONG, block SHORT.**
- **BEARISH:** price < EMA200 **and** EMA50 < EMA200 → **allow SHORT, block LONG.**
- **NEUTRAL:** anything else → **allow both.**
- **UNKNOWN:** any data error / < 200 bars → **fail-safe, allow both.**

### 2.4 Score system (`_strength_score`, 0–4)

+1 for each of: RSI extreme (`<20` or `>80`); strong BB penetration (the *close*
pierces the band, not just a wick); BTC strongly aligned with the trade side;
good spread **and** liquidity (spread < half the max **and** last volume ≥ avg).

### 2.5 Signal priority

There is **no ranking/priority between symbols.** `_evaluate_signals` iterates
the fixed symbol list in list order and fans out each qualifying signal
independently. Whichever accounts and symbols pass their own gates get trades;
there is no "best signal wins" arbitration.

### 2.6 Final approval (Stage B — `PositionManager.open_position`)

Strict order (all **per-account**):

1. **BotControl gate** — `can_open_trades()` (bot_enabled & not emergency & not force-close).
2. **Supported-symbol** guard.
3. **Tier resolution** — balance selects a tier; below `min_tier_balance` ($20) → skip `balance_below_min_tier`.
4. **Risk locks** — refresh from realised PnL, then `can_take_new_entry()`:
   PROTECTION lock → emergency shutdown → daily-profit lock → daily-loss lock.
5. **Cooldown** — `cooldown_<symbol>` remaining > 0 → skip.
6. **Structural limits** (per-tier):
   - one basket per symbol (`existing_basket_on_symbol`),
   - `max_active_symbols` (Tier1 **4** / Tier2 **6**),
   - `max_positions` = Σ layer_count (Tier1 **8** / Tier2 **12**).
7. **Correlation protection** — required score rises with open baskets:
   `0 active → score ≥ 2`, `1+ active → score ≥ 3`. *(Note: applied to **all**
   open baskets, not a specific correlated subset — see Section 13.)*
8. Fetch market info; **size at a fresh execution-time ticker price**; build the
   order via `PositionSizer` — unsuitable sizing → skip `sizing_unsuitable`.
9. Set cross margin + leverage, place the market order, resolve the **actual
   fill** (partial-fill safe), persist the basket, log `OPEN`.

---

## SECTION 3 — POSITION SIZING

### 3.1 Tier system (`config/settings.py:account_tiers`, balance ONLY selects the tier)

| | **Tier 1** | **Tier 2** |
|---|---|---|
| Balance band | $20 – $39.99 | ≥ $40 |
| Layer-1 margin | **$1** | **$2** |
| Layer-2 margin | **$2** | **$4** |
| Max basket exposure | **$3** | **$6** |
| Fixed-USD TP (L1 / L1+L2) | **$0.30 / $0.80** | **$0.50 / $1.20** |
| ROI target (L1 / recovery) | **12% / 10%** | **10% / 10%** |
| Daily profit target | **+$2.00** | **+$3.50** |
| Daily loss limit | **−$3.00** | **−$4.00** |
| Max active symbols / positions | **4 / 8** | **6 / 12** |
| Protection floor (death) | **$15** | **$30** |

- **Sizing is FIXED and never balance-scaled.** No percentage sizing, no
  martingale, no volatility sizing. Balance is read *only* to pick a tier.
- **Tier lock:** once a basket opens, its tier id is stored in the basket's
  `volatility` column and *locked* — later balance changes (deposits/withdrawals
  crossing a boundary) never resize an open basket's margin, cap, or TP target.
- Max **total deployed margin** per account: Tier 1 = 4×$3 = **$12**;
  Tier 2 = 6×$6 = **$36**.

### 3.2 Margin → quantity (`risk/position_sizer.py:build_order`)

```
notional = margin × leverage
quantity = floor_to_lot_step( notional / price )
```

### 3.3 Leverage

- Default **8×** (`default_leverage`). Admin `leverage_override` clamped to
  `[min_leverage, max_leverage]` = **[5×, 10×]**, then to `hard_max_leverage`
  (**10×**). Fixed — never dynamically adjusted or balance-scaled.

### 3.4 Minimum-notional & exchange-safety validation (`build_order`)

An order plan is `suitable=False` (rejected + logged reason) if:
- price ≤ 0 or leverage ≤ 0;
- quantity rounds to **zero** at the lot step;
- quantity **< exchange min quantity**;
- **notional < min notional** (`limits.cost.min` or `min_notional_floor` $5;
  `validate_min_notional` uses a hard $5 default).

`min_notional_floor = $5`. Config validation guarantees `layer1_margin ×
default_leverage ≥ 5` so the smallest entry clears the dust floor
(Tier 1: $1×8 = $8 ✓).

### 3.5 Rejected-order handling

- A rejected sizing plan → `_skip(..., 'sizing_unsuitable (<reason>)')`, no order placed.
- An exchange exception during placement → `_skip(..., 'order_error (...)')`, returns `None`.
- **Partial fills are honoured, never assumed full:** `_resolve_fill` reads the
  actual `filled`/`amount` and average price and recomputes the *actual* margin
  (`filled × price / leverage`). Basket TP and exposure are derived from the real
  fill. Zero fill → `no_fill` skip.

---

## SECTION 4 — STOP LOSS SYSTEM

The bot has **three distinct loss backstops**, layered from tightest to broadest.
There is **no exchange-native stop order** — all stops are evaluated in software
each management cycle and executed as reduce-only market closes.

### 4.1 Per-basket hard stop-loss (`basket_sl`) — the primary SL

- **File / function:** `grid/take_profit.py:evaluate_exit` + `check_basket_sl`;
  executed by `grid/position_manager.py:_close_basket_sl`.
- **Value:** `basket_hard_sl_usd = $0.30` (config). Applies to Layer-1 and
  recovery baskets, every symbol.
- **Type:** **net dollar loss.** `net_pnl = gross_unrealized − round_trip_fees`.
  Trips when `net_pnl ≤ −0.30`.
- **Fees:** `qty × price × taker_fee_pct(0.0004) × 2` (round trip, taker both legs).
- This is a *per-basket* cut that sits **below** the account-level daily-loss and
  death-protection guards — it fires earlier so one basket can't eat the whole
  daily allowance, but it never weakens the account guards.

### 4.2 Daily loss limit (account-level) — `risk/risk_manager.py:check_loss_limit`

- **Basis:** daily **trading** PnL = today's realised (closed trades) + current
  open unrealised, **never wallet balance** (deposits/withdrawals excluded).
- **Threshold:** per-tier (`daily_loss_limit` Tier1 **−$3** / Tier2 **−$4**).
- **Action:** latch `daily_loss_locked=true`, **close ALL baskets**, block new
  entries until the next UTC day.

### 4.3 Account death protection (equity floor) — `check_account_death_protection`

- **Basis:** **equity = wallet balance + total open floating PnL.**
- **Threshold:** per-tier `protection_floor` (Tier1 **$15** / Tier2 **$30**).
- **Action:** latch `protection_locked=true` (**PERMANENT** — survives UTC reset,
  admin-only clear via `python main.py --clear-protection <id>`), close ALL
  baskets, log `PROTECTION_LOCKED`. Highest priority guard.

### 4.4 When & how often SL is checked

- **Every management cycle** (main loop, ~10 s) inside `manage_baskets`, per
  account, per basket. Death protection and daily-loss are checked *before* the
  per-basket loop; `basket_sl` is checked inside `evaluate_exit` for each basket.

### 4.5 Exact SL execution & aftermath

- `basket_sl`: `_close_basket_sl` logs `BASKET_SL_HIT` then `close_basket(..., 'basket_sl')`.
- `close_basket`: idempotent (lock + `_closing` set), fetches current price,
  places a **reduce-only** counter market order for the full quantity, retrying
  up to **3 times** and continuing on partial closes; benign "already flat"
  errors are treated as closed. On success it writes a `TradeRecord` (with
  net PnL = gross − round-trip fee), marks the basket closed, **starts the
  symbol cooldown**, and logs `CLOSE`.
- Daily-loss / death close use `close_all_baskets(...)` (loops `close_basket`).

---

## SECTION 5 — TAKE PROFIT SYSTEM

### 5.1 The three profit conditions (`grid/take_profit.py:evaluate_exit`)

Evaluated in this order; **first hit wins**. All use **net** PnL (gross − round-trip fees).

1. **ROI target** (`roi_l1` for a 1-layer basket, `roi_recovery` for ≥2 layers):
   `roi = net_pnl / total_margin`; trip when `roi ≥ roi_target`.
   - Targets from the basket's **locked tier**, overridable per symbol via
     `roi_targets_for()` (e.g. **TRX → 8%/8%**).
   - Because the ROI dollar value is *below* the fixed-USD target, ROI usually
     fires first — closing profitable baskets earlier to free capital.
2. **Fixed-USD target** (`basket_tp`): trip when `net_pnl ≥ target_usd`
   (`basket_tp_l1` for 1 layer / `basket_tp_l2` for ≥2 layers, from the locked tier).
3. **Hard SL** (`basket_sl`, the loss branch): `net_pnl ≤ −basket_hard_sl_usd`.

`Margin %` = the ROI metric. `Dollar target` = the fixed-USD metric. `Net profit`
= gross − fees. `Fees` = `qty × price × 0.0004 × 2`.

### 5.2 TP Lock — the exit-execution guarantee (`position_manager.py`)

The distinguishing mechanism. Once a profit exit is decided, the decision is
**frozen and persisted** so a post-target price reversal can never leave a
profitable basket open.

- **Activation** (`_activate_tp_lock`): on the *first* cycle a profit exit fires
  (`roi_l1`/`roi_recovery`/`basket_tp`), persist `account_<id>_tp_lock_<basket>`
  = the exit reason (+ `_time`). Idempotent — logs `TP_LOCK_ACTIVATED` once.
- **Detection** (`_tp_lock_reason`): at the top of each basket's turn in
  `manage_baskets`, if a lock is set the basket **skips all re-evaluation** of
  targets/price and goes straight to closing.
- **Execution** (`_execute_tp_locked_close`): calls `close_basket`. If the basket
  is confirmed flat → release the lock, log `TP_LOCK_EXECUTED`. If not (exchange
  reject / partial / network) → **keep the lock**, log `TP_LOCK_RETRY`, retry
  next cycle. Persistence means it survives a **process/server restart or crash**.
- **Partial fills / retry:** `close_basket` itself retries 3× and continues on
  partial closes; the TP lock adds the *across-restart* guarantee on top.
- **Startup audit** (`engine.start`): `tp_lock_consistency_report()` reports all
  locks; `cleanup_orphan_tp_locks()` releases locks whose basket is already
  closed/missing so a stale lock can never wedge.

### 5.3 Reconcile → final close (`reconcile_baskets` / `_finalize_reconciled_basket`)

Before each management pass, `reconcile_baskets` fetches live exchange positions.
A DB basket **older than 60 s** with **no matching live position** (closed
externally, lost fill write, or liquidation) runs the full closure workflow:
resolve exit reason (committed TP-lock reason if held, else `reconciled`), write
a `TradeRecord` at best-available mark, release any orphan TP lock, start the
cooldown, log `RECONCILE_CLOSE`.

### 5.4 Exact TP sequence

```
manage_baskets(basket)
  ├─ TP lock set? ─yes─► _execute_tp_locked_close ─► close_basket (retry/partial-safe)
  │                         └─ flat? release lock + TP_LOCK_EXECUTED : keep lock + retry
  └─no─► evaluate_exit(basket, price)
           ├─ roi_l1 / roi_recovery / basket_tp ─► _activate_tp_lock ─► _execute_tp_locked_close
           ├─ basket_sl ─► _close_basket_sl ─► close_basket
           └─ None ─► check recovery trigger ─► maybe add Layer 2 ─► persist ─► keep basket
```

---

## SECTION 6 — POSITION MANAGEMENT

- **Monitoring:** `SignalExecutor.manage_all_accounts()` runs every loop
  (~10 s) on a thread pool. Per account: build components → `load_active_baskets`
  → fetch live balance → `risk_manager.initialize(balance)` → `reconcile_baskets`
  → `manage_baskets`.
- **Unrealised PnL** (`core/dto.py:Basket.unrealized_pnl`): uses the
  quantity-weighted **avg entry price** across active layers:
  long → `(price − avg) × qty`; short → `(avg − price) × qty`.
- **Frequency:** every ~10 s (exit path). Signal generation only every ~30 s.
- **Synchronisation / reconciliation — two independent paths:**
  1. **Trading path** — `reconcile_baskets` (drops DB baskets with no live
     exchange position, runs full closure). This is the one that affects trading.
  2. **Bookkeeping path** — `SyncService._reconcile_positions` (background, every
     60 s) maintains the `positions` table (a *parallel* record used by the admin
     API / risk-metric snapshots, **not** consumed by the trading logic).
- **Price snapshot:** `manage_baskets` fetches one ticker per symbol per cycle
  and reuses it; missing tickers are retried (`_fetch_price_with_retry`, 3×) so a
  transient hiccup never defers a basket that may be due to close.

---

## SECTION 7 — PORTFOLIO PROTECTION

Priority order enforced inside `manage_baskets` (survival first):

| # | Protection | Trigger | Basis | Action on activation |
|---|---|---|---|---|
| **0** | **Death protection / Protection Lock** | equity < tier floor ($15 / $30) | wallet balance + floating PnL | Close ALL baskets; latch `protection_locked` **permanently** (admin reset only). |
| **1** | **Daily Loss Lock** | daily trading PnL ≤ −limit (−$3 / −$4) | realised + unrealised (no wallet) | Close ALL baskets; block new entries until next UTC day. |
| **2a** | **TP Lock** (per basket) | profit exit already committed | frozen decision | Keep closing until flat (Section 5.2). |
| **2b** | **Basket TP / Basket SL** | ROI/USD target or net ≤ −$0.30 | net PnL | Close that basket. |
| **3** | **Daily Profit Lock** | daily trading PnL ≥ target (+$2 / +$3.5) | realised + unrealised | **Block new entries only** — existing baskets keep being managed. |

- **"Portfolio Profit Lock" / "Daily Profit Lock"** = the same `daily_profit_locked`
  latch (blocks new entries, no closing).
- **"Daily Loss Lock"** = `daily_loss_locked` (closes all, blocks entries).
- **"Death Protection" / "Protection Lock"** = `protection_locked` (permanent).
- **"Dynamic trailing protection":** *not implemented* as a trailing stop. The
  closest behaviour is the ROI-first exit (closes earlier than the USD target)
  and the persistent TP lock. There is **no high-water-mark trailing exit** —
  `high_water_mark` is tracked but only used for risk-metric drawdown reporting
  (see Section 13).

**After activation:** protection/daily-loss locks are checked again in
`can_take_new_entry` on every open attempt, so re-entry stays blocked. Daily
locks clear at the next UTC reset (`_begin_new_day`); the protection lock does not.

---

## SECTION 8 — RISK MANAGEMENT (every layer)

**Validations / rejections in the entry path** (each logs `ENTRY_SKIP`):
bot_control_disabled, unsupported_symbol, balance_below_min_tier,
PROTECTION_LOCKED, emergency shutdown, daily profit locked, daily loss locked,
cooldown, existing_basket_on_symbol, max_active_symbols, max_positions,
correlation_protection, market_info_error, sizing_unsuitable, no_fill, order_error.

**Exposure & position limits:**
- One basket per symbol (`max_basket_per_symbol=1`, enforced by the
  existing-basket check).
- Per-tier `max_active_symbols` and `max_positions` (Σ layer_count).
- **Recovery exposure cap** (`_add_recovery_layer`): uses *intended* tier margins
  (`layer1_margin + layer2_margin`) vs `max_basket_exposure`, so a legitimate
  2-layer basket is never falsely blocked by fill-price drift; only a genuine
  misconfiguration is blocked.
- Max total deployed margin: Tier 1 **$12**, Tier 2 **$36** (structural).

**Daily limits** (per-account, per-tier, UTC-reset): profit target (blocks
entries), loss limit (closes all + blocks). Both derived from *trading* PnL only.

**Emergency stops — two separate concepts:**
1. **BotControl.emergency_stop** (control plane): halts scanner/signals/new
   orders/recovery, cancels pending orders, **leaves TP/SL active**, does *not*
   force-close. Set via admin API or `EMERGENCY_STOP` env var.
2. **RiskManager emergency_shutdown** (DB state `emergency_shutdown`): checked in
   `can_take_new_entry`; cleared via `python main.py --clear-shutdown`.
   **Note:** `trigger_emergency_shutdown` exists but is **never called** anywhere
   in the code — this shutdown latch has no automatic trigger (Section 13).

**FORCE_CLOSE_ALL:** admin one-shot — disables new trades, closes every basket on
every managed account (reason `force_close_all`), then resets the flag.

---

## SECTION 9 — COOLDOWN SYSTEM

- **Per-symbol, per-account.** Key: `account_<id>_cooldown_<symbol>` in `bot_state`.
- **Start:** `_finalize_closed_state` writes `time.time()` whenever a basket
  closes (any reason — TP, SL, reconcile, force-close).
- **Duration:** `symbol_cooldown_seconds = 900 s` (15 min = one 15m candle).
- **End:** `_cooldown_remaining` = `window − (now − closed_at)`; entries on that
  symbol are skipped while > 0.
- **No separate "global cooldown"** — there is only the per-symbol cooldown plus
  the 30 s signal-eval throttle at the engine level (that throttle is a pacing
  mechanism, not a per-account cooldown).

---

## SECTION 10 — TRADE LIFECYCLE (timeline)

```
Engine loop (10s)
   └─ signal throttle (30s) ─► SignalEngine.generate_signal(symbol) [15m data]
         ├─ data/warmup guards
         ├─ pre-trade risk filters (spread, ATR explosion, news, volume)
         ├─ RSI + Bollinger touch  ─► side
         └─ BTC trend filter       ─► Signal (+ strength_score 0–4)
   └─ SignalExecutor.execute_signal(signal)
         ├─ BotControl gate
         ├─ save signal (audit)
         ├─ eligibility gate (user active + subscription active + account active)
         └─ per eligible account (thread pool):
               PositionManager.open_position(signal, balance)
                 ├─ tier resolve (balance→tier)
                 ├─ risk locks (protection/emergency/daily profit/daily loss)
                 ├─ cooldown
                 ├─ structural limits (1/symbol, max symbols, max positions)
                 ├─ correlation score gate
                 ├─ PositionSizer.build_order (min-notional / lot-step safety)
                 └─ place market order ─► resolve actual fill ─► persist Basket (Layer 1)
                                        ─► execution_logs + OPEN log
Monitoring (every 10s) ─► reconcile_baskets ─► manage_baskets:
   ├─ [0] death protection  ─► close all (permanent lock)
   ├─ [1] daily loss limit  ─► close all
   ├─ [2a] TP lock (frozen) ─► close & retry until flat
   ├─ [2b] evaluate_exit ─► roi_l1 / roi_recovery / basket_tp ─► TP lock ─► close
   │                    └─► basket_sl ─► close
   ├─ [3] recovery trigger (ATR×2 OR L1 loss ≥ $0.30) ─► add Layer 2
   └─ (daily profit latch blocks NEW entries)
Close ─► TradeRecord (net PnL) ─► `trades` table ─► start symbol cooldown (900s)
Cooldown (15m) ─► symbol blocked ─► expires ─► symbol eligible again
Daily reset (UTC 00:00) ─► clear daily locks, save daily stats, new start balance
```

---

## SECTION 11 — CONFIGURATION (`config/config.json` → `Settings`)

| Key | Controls | Default | Read by |
|---|---|---|---|
| `use_testnet` | testnet vs live (per-account override too) | `false` | ExchangeClient, main |
| `supported_symbols` | the *only* pairs traded (fixed watchlist, 20 pairs) | 20 alt perps | SignalEngine, engine, PositionManager |
| `btc_symbol` | BTC pair for the trend filter | `BTC/USDT:USDT` | SignalEngine |
| `timeframe` / `candle_limit` | candle interval / bars fetched | `15m` / `300` | SignalEngine, btc_regime |
| `loop_interval_seconds` | main loop cadence (exits) | `10` | engine |
| `signal_eval_interval_seconds` | new-entry throttle | `30` | engine |
| `rsi_period/oversold/overbought` | RSI trigger | `14/30/70` | SignalEngine |
| `bb_period/bb_std` | Bollinger bands | `20/2.0` | SignalEngine, indicators |
| `atr_period` | ATR | `14` | SignalEngine, recovery, filters |
| `btc_filter_enabled/ema_fast/ema_slow/cache` | BTC trend gate | `true/50/200/300s` | SignalEngine, btc_regime |
| `default/min/max/hard_max_leverage` | leverage & clamps | `8/5/10/10` | Settings, PositionSizer |
| `min_tier_balance` | min balance to trade | `20.0` | Settings.get_tier, PositionManager |
| `account_tiers` | all fixed sizing/limits/targets | Tier1/Tier2 (Sec 3.1) | everywhere |
| `min_notional_floor` | dust-order floor | `5.0` | PositionSizer, utils |
| `basket_hard_sl_usd` | per-basket net SL | `0.30` | TakeProfitManager |
| `symbol_roi_overrides` | per-symbol ROI (TRX 8%/8%) | TRX | Settings.roi_targets_for |
| `recovery_max_layers` | max layers/basket | `2` | recovery, PositionManager |
| `layer2_atr_multiplier` | ATR spacing for Layer 2 | `2.0` | recovery |
| `recovery_loss_trigger_usd` | L1 loss that triggers Layer 2 | `0.30` | recovery |
| `max_basket_per_symbol` | baskets per symbol | `1` | PositionManager |
| `correlation_min_score_first/additional` | score gate 0/1+ open | `2/3` | PositionManager |
| `risk_filter_lookback` | bars for ATR/vol averages | `30` | SignalEngine |
| `max_spread_pct` | spread ceiling | `0.0010` | SignalEngine |
| `atr_explosion_multiplier` | ATR spike skip | `2.5` | SignalEngine |
| `news_candle_atr_multiplier` | candle-body skip | `2.5` | SignalEngine |
| `volume_spike_multiplier` | volume skip | `3.0` | SignalEngine |
| `symbol_cooldown_seconds` | per-symbol cooldown | `900` | PositionManager |
| `taker_fee_pct` | fee for net-PnL estimation | `0.0004` | TakeProfit, close |
| `database_url` / `log_level` | infra | Postgres / INFO | Database, engine |

**Env vars (override config / control plane):** `DATABASE_URL`,
`MASTER_ENCRYPTION_KEY` (required to trade), `ADMIN_API_KEY`, `ADMIN_API_PORT`,
`BOT_ENABLED`, `MANAGE_EXISTING_POSITIONS`, `FORCE_CLOSE_ALL`, `EMERGENCY_STOP`.
Control-plane flags reset to env defaults on restart (intentional).

---

## SECTION 12 — FILE RESPONSIBILITIES

| File | Purpose | Main classes | Key functions | Depends on |
|---|---|---|---|---|
| `main.py` | CLI entry; loads .env/config; admin flags | — | `main`, `--clear-shutdown`, `--clear-protection`, `--api` | Settings, Database, TradingEngine |
| `config/settings.py` | Single source of truth; tiers; validation | `Settings`, `Side`, `BtcRegime` | `load`, `create_account_settings`, `get_tier`, `roi_targets_for`, `validate` | — |
| `core/engine.py` | Main loop; logging; lifecycle; API launch | `TradingEngine`, `AccountFilter` | `start`, `_run_loop`, `_evaluate_signals` | Database, ExchangeClient, SignalEngine, SignalExecutor, SyncService, BotControl |
| `core/database.py` | Postgres repository (all persistence) | `Database` | baskets/trades/state/accounts/eligibility, TP-lock maintenance | models, dto, SQLAlchemy |
| `core/models.py` | ORM tables | `UserModel`, `AccountModel`, `BasketModel`, `RecoveryLayerModel`, `TradeModel`, `SignalModel`, `BotStateModel`, `SubscriptionModel`, `PositionModel`, `RiskMetricModel`, `ExecutionLogModel`, `DailyStatModel`, `WatchlistModel` | — | SQLAlchemy |
| `core/dto.py` | In-memory dataclasses | `Basket`, `RecoveryLayer`, `Signal`, `TradeRecord`, `CoinScore` | `avg_entry_price`, `unrealized_pnl`, `close_all` | — |
| `signals/signal_engine.py` | Entry signals + skip filters | `SignalEngine` | `generate_signal`, `get_btc_regime`, `_risk_filter_reason`, `_strength_score` | indicators, btc_regime, ExchangeClient |
| `signals/indicators.py` | Pure TA math | — | `compute_rsi/ema/sma/atr/bollinger_bands` | pandas, numpy |
| `signals/btc_regime.py` | BTC trend gate | — | `classify_btc_regime`, `regime_allows_side` | indicators |
| `grid/position_manager.py` | Basket lifecycle (open/manage/close/reconcile/cooldown/TP-lock) | `PositionManager` | `open_position`, `manage_baskets`, `close_basket`, `reconcile_baskets`, `_activate_tp_lock`, `_execute_tp_locked_close` | TakeProfit, Recovery, RiskManager, PositionSizer, ExchangeClient, dto |
| `grid/take_profit.py` | Exit math (ROI/USD/SL) | `TakeProfitManager` | `evaluate_exit`, `net_pnl`, `basket_roi`, `check_basket_tp/sl` | Settings, dto |
| `grid/recovery.py` | Layer-2 hybrid trigger | `RecoverySystem` | `check_recovery_trigger`, `layer2_distance`, `build_layer` | Settings, dto |
| `risk/risk_manager.py` | Daily limits + death protection + emergency latch | `RiskManager` | `can_take_new_entry`, `check_loss_limit`, `update_profit_target`, `check_account_death_protection` | Settings, DB wrapper |
| `risk/position_sizer.py` | Margin→quantity + exchange safety | `PositionSizer` | `build_order` | utils, Settings |
| `execution/executor.py` | Multi-account fan-out + isolation + component cache | `SignalExecutor`, `AccountDatabaseWrapper`, `ExecutionResult` | `execute_signal`, `manage_all_accounts`, `_build_account_components`, `cancel_all_pending_orders` | AccountManager, all per-account managers |
| `exchange/client.py` | ccxt Binance wrapper + retries | `ExchangeClient` | `initialize`, `fetch_ohlcv/ticker/balance/positions`, `place_market_order`, `close_position` | ccxt, pandas |
| `exchange/utils.py` | Rounding / min-notional | — | `round_quantity`, `validate_min_notional`, `calculate_margin_required` | math |
| `control/bot_control.py` | Control-plane singleton | `BotControl`, `ControlSnapshot` | `can_open_trades`, `can_manage_positions`, `can_add_recovery_layer`, `set_emergency_stop`, `request_force_close_all` | os, threading |
| `accounts/manager.py` | Account CRUD + credential validation | `AccountManager` | `create_account`, `decrypt_account_keys`, `get_active_accounts` | encryption, ExchangeClient |
| `accounts/encryption.py` | Fernet encrypt/decrypt | `EncryptionService`, `EncryptionError` | `encrypt`, `decrypt` | cryptography |
| `services/sync.py` | Background balance/position/risk sync | `SyncService` | `_sync_loop`, `sync_balances`, `sync_positions`, `update_risk_metrics` | AccountManager, ExchangeClient, Database |
| `admin/*` | FastAPI admin REST API | `create_app`, routers | accounts/positions/trades/bot-control endpoints | Database, BotControl, SignalExecutor |

---

## SECTION 13 — KNOWN LIMITATIONS (identification only, no redesign)

**A. Stale docstrings vs actual config (documentation drift — behaviour follows the CODE):**
- `grid/take_profit.py` header says Tier1 TP $0.50/$1.50, Tier2 $0.80/$2.00 and
  SL default −$0.50 — **actual** is Tier1 $0.30/$0.80, Tier2 $0.50/$1.20, SL −$0.30.
- `risk/risk_manager.py` header says "Daily Profit Target ($5) / Daily Loss ($3)"
  — **actual** is per-tier +$2/+$3.5 and −$3/−$4.
- `risk/position_sizer.py` says Tier1 L1$2/L2$4, Tier2 L1$4/L2$8, leverage 3×–8×
  default 5× — **actual** Tier1 $1/$2, Tier2 $2/$4, leverage 5×–10× default 8×.
- `grid/position_manager.py` class docstring says max symbols "Tier 1: 2, Tier 2: 3"
  — **actual** 4 and 6.
- `create_account_settings` docstring says leverage "3×–8×" — code clamps to
  `[min_leverage=5, max_leverage=10]`.
- `recovery.py` says loss trigger default $0.50 — **actual** $0.30.
  → *Risk:* anyone relying on docstrings will mis-state the live risk profile.

**B. "Correlation protection" is a misnomer.** The docstrings claim TRX/XRP/XLM
are a correlated group, but the code counts **all** open baskets
(`active_baskets`) regardless of symbol correlation. It's really a global
"the more baskets open, the higher the signal score required" gate. Not a bug,
but the name/logic don't match the intent implied by the docs.

**C. `RiskManager.trigger_emergency_shutdown` is dead code.** The DB
`emergency_shutdown` latch is *checked* in `can_take_new_entry` and can be
cleared from the CLI, but nothing ever *sets* it programmatically. Automatic
emergency shutdown is effectively unwired. (The separate `BotControl.emergency_stop`
control-plane flag *is* wired via the admin API.)

**D. Two parallel emergency/position systems.** `BotControl.emergency_stop`
(control plane) and `RiskManager.emergency_shutdown` (DB latch) are unrelated;
likewise the trading-path `reconcile_baskets` vs the bookkeeping-path
`SyncService._reconcile_positions`/`positions` table. The `positions` table and
`risk_metrics` snapshots are maintained but **not consumed by trading decisions**
— potential for confusion / silent divergence.

**E. Dead / vestigial "scanner" artifacts.** `CoinScore` DTO, `WatchlistModel`,
`save_watchlist`/`get_watchlist`, and `get_all_futures_symbols` remain, but the
dynamic scanner was removed in favour of a **fixed 20-symbol list**. Comments
throughout still reference a "CoinScanner" that no longer exists.

**F. `SyncService.sync_trades` is a no-op stub** — logs intent and returns.
Trade history is written only at basket-close time; there is no exchange-side
trade reconciliation.

**G. `high_water_mark` trailing is not an exit.** HWM is tracked and used for
drawdown reporting only; there is no trailing-stop / profit-giveback exit despite
"dynamic trailing protection" language in the requirements.

**H. Software-only stops (latency/availability risk).** All SL/TP are evaluated
in the ~10 s loop and executed as market closes. There are **no exchange-native
stop or TP orders**. If the process is down (and only restarts recover the TP
lock), a basket sits unmanaged until the loop resumes. Liquidation is the only
exchange-side backstop.

**I. `--mode backtest` advertised but unavailable.** `main.py` epilog shows a
backtest example, but `--mode` `choices=['live']` — backtest mode does not exist.

**J. Per-cycle network cost.** `manage_baskets` fetches a ticker per symbol and
`reconcile_baskets` calls `fetch_positions` per account every ~10 s; with many
accounts this is the dominant load (partly mitigated by the component cache, but
balances/positions are still fetched every cycle).

**K. Config-vs-code fee realism.** Net PnL and the exit targets assume a flat
`taker_fee_pct = 0.0004` round trip and ignore funding costs — baskets that stay
open across funding intervals (e.g. TRX, the reason for its 8% override) accrue
untracked funding cost not reflected in the net-PnL / SL math.

---

## SECTION 14 — FINAL SUMMARY

- **Current strategy:** A conservative, fixed-size **mean-reversion basket
  recovery** system ("Dark-Venus / ZenGrid") on Binance USDT-M perps, 15m
  timeframe, across a fixed universe of 20 liquid alt-coins, gated by a global
  BTC trend filter. Multi-account SaaS: one strategy, identically applied to
  every subscriber account, fully isolated per account.

- **Current entry style:** Countertrend mean-reversion — **RSI(14) extreme (<30
  / >70) + Bollinger-band touch**, filtered by BTC 15m trend direction and five
  pre-trade skip filters (spread, ATR explosion, news candle, volume spike, data
  quality), then per-account risk/structural/correlation/cooldown gates. Sizing
  is **fixed by balance tier**, never scaled.

- **Current exit style:** Basket-level, **net-of-fees**, whichever fires first —
  ROI target, fixed-USD target, or per-basket hard SL — all evaluated every ~10 s
  and executed as reduce-only market closes. A **persistent TP lock** guarantees
  a hit profit target is seen through to a flat close even across restarts.

- **Current TP logic:** ROI-first (Tier1 12%/10%, Tier2 10%/10%, TRX 8%) so
  profitable baskets close early to free capital; fixed-USD backstop
  (Tier1 $0.30/$0.80, Tier2 $0.50/$1.20); reconcile finalises vanished positions.

- **Current SL logic:** Three layers — per-basket net SL (**−$0.30**), account
  daily loss limit (**−$3 / −$4**, closes all), and permanent equity-floor death
  protection (**$15 / $30**). No exchange-native stops.

- **Current portfolio protection:** Priority-ordered — death protection (0) →
  daily loss lock (1) → TP lock / basket TP·SL (2) → daily profit lock (3, blocks
  new entries only). Daily locks reset at UTC midnight; death protection is
  permanent (admin reset).

- **Current risk management:** FIXED sizing (no martingale, max **2 layers**/basket,
  ATR-spaced recovery), per-tier symbol/position caps, hard leverage ceiling 10×,
  min-notional/lot-step safety, partial-fill-safe fills, per-symbol 15-min
  cooldown, correlation score gate, and a control plane (bot enable/disable,
  emergency stop, force-close-all) driven by env vars + admin API.

- **Current trading philosophy:** *Survival → drawdown control → consistency →
  profit.* Small, fixed, capped bets; buy dips / sell rips against short-term
  extremes but only *with* the BTC trend; recover a losing basket **once** with a
  single controlled layer; take modest net profits early; and cut/lock hard at
  the basket, daily, and account-equity levels so no single trade, day, or
  account drawdown can spiral.
```
