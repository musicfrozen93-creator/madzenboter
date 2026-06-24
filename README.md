# ZenGrid Futures Core — Single-Entry Scalping Bot

A lightweight, survival-first **single-entry scalping** core for Binance USDT-M
Futures. It trades a **fixed universe of 100 liquid USDT-M perpetuals** on the
15-minute timeframe using mean-reversion entries, a fixed take-profit and
stop-loss expressed as a percentage of the position margin, and one position per
symbol. **No recovery, no Layer 2, no averaging down, no martingale, no grid.**

The priority order is:

1. **Survival**
2. **Drawdown control**
3. **Consistency**
4. **Profit**

---

## Architecture at a glance

| Setting | Value |
|---------|-------|
| Style | Single entry — **one position per symbol** |
| Universe | **Fixed 100 USDT-M perps** (scans only these) |
| Timeframe | **15m** |
| Leverage | **10×** (admin override 8×–10×, hard cap 10×) |
| Take-profit | **20% of margin** (net) |
| Stop-loss | **12% of margin** (net) |
| Portfolio profit lock | per-account trailing flatten (per-tier) |
| BTC | trend **filter only** (never traded) |
| ETH | **excluded** (min-notional too high for tier margins) |

### Account tiers (balance only selects the tier)

| Setting | Tier 1 ($20–$39.99) | Tier 2 ($40+) |
|---------|---------------------|----------------|
| Margin per trade | $0.8 | $1.5 |
| Position notional @10× | $8 | $15 |
| Take-profit (20%) | $0.16 | $0.30 |
| Stop-loss (12%) | $0.096 | $0.18 |
| Max positions | 8 | 10 |
| Daily profit target | $2 | $3.5 |
| Daily loss limit | $3 | $4 |
| Portfolio lock — arm / floor (dynamic trail) | $0.50 / $0.35 | $0.80 / $0.50 |
| Death-protection floor (equity) | $15 | $30 |

Sizing is fixed within a tier: no balance scaling, no percentage sizing, no
dynamic/volatility sizing, no martingale. The tier is **locked onto the position
at open** and never changes if the balance later crosses a boundary.

---

## Fixed universe (100 symbols)

The bot trades **only** the 100 symbols in `supported_symbols` (see
`config/config.json` / `config/settings.py::SUPPORTED_UNIVERSE`) and **ignores
every other Binance market**. The set is curated for high liquidity, tight
spreads, medium volatility, and a **5-USDT minimum notional** so the tier
margins always clear it. **BTC and ETH are excluded from execution** (BTC is the
regime-filter reference only).

At startup the engine validates each symbol against the exchange and **drops**
any that are delisted/inactive or whose minimum notional exceeds the Tier-1
position notional (logged as `UNIVERSE_VALIDATED`). It **never adds**
replacements — the universe stays fixed at whatever subset of the 100 is
currently tradeable.

---

## Strategy

**Trading style:** mean-reversion scalp · single entry · fixed TP/SL · cooldown.

### BTC trend filter (runs before every new position)

Uses `BTCUSDT` 15m:

| BTC state | Condition | Effect |
|-----------|-----------|--------|
| Bullish | price > EMA200 **and** EMA50 > EMA200 | block SHORT |
| Bearish | price < EMA200 **and** EMA50 < EMA200 | block LONG |
| Neutral | otherwise | allow both |

### Entry logic (all conditions must be true)

- **LONG:** RSI(14) < 30 **and** price touches the lower Bollinger band **and** BTC filter approves.
- **SHORT:** RSI(14) > 70 **and** price touches the upper Bollinger band **and** BTC filter approves.

### Pre-trade filters (skip the trade if any trips)

- Spread too high (`> max_spread_pct`)
- ATR explosion (current ATR > 2.5× average ATR)
- News / oversized candle (candle body > 2.5× ATR)
- Volume spike (> 3× average volume)
- **ATR feasibility band** — skip unless `0.30% ≤ ATR/price ≤ 1.20%` so the fixed
  % stop is never inside noise and the fixed % target stays reachable in time.
- **Signal quality** — a strength score (0–4: extreme RSI, strong BB penetration,
  BTC aligned, good spread/liquidity) must be ≥ `min_signal_score` (default 1).

### Exit logic (single entry, the FIRST to fire)

Both targets are evaluated on **net** PnL (gross − round-trip taker fees):

1. **Take-profit** — net ≥ `tp_margin_pct × margin` (20%) → reason `tp`.
2. **Stop-loss** — net ≤ −`sl_margin_pct × margin` (12%) → reason `sl`.

**Immediate TP execution:** the moment the TP condition is true the bot — in the
**same management cycle** — logs `TP_DETECTED`, activates + persists the **TP
lock**, and submits the close (`TP_CLOSE_SENT` → `TP_CLOSE_CONFIRMED`). It does
not wait for a later cycle and does not re-evaluate TP, so a position that hit
its target can never keep running. The lock is only released after a confirmed
flat close.

The account-level guards outrank everything, in order:

- **P0 — Account death protection:** equity < tier floor → permanent
  `protection_lock` + close all positions (admin reset only).
- **P1 — Daily loss limit:** realised + unrealised ≤ −tier limit → close all + lock until next UTC day.
- **P1.5 — Portfolio trailing profit lock:** see below.
- **P2 — Position exit:** take-profit (TP lock + close) or stop-loss (`sl`).

Daily profit target latches a new-entry lock (no closing).

### Portfolio trailing profit lock (per-account, dynamic)

A per-account aggregate profit protector with a **dynamic trailing** stop. When
the account's **total open unrealised PnL** reaches the tier **arm trigger** the
lock arms and stores the running **peak**. From then on it protects

```
protected = max(floor, peak × protection%)
```

where the protection percentage **ratchets up** as the peak grows. The moment
current profit falls **below** the protected level, all positions are closed with
reason `portfolio_profit_lock`. The protected level never falls.

| Tier | Arm at | Protection bands (peak → % protected) | Floor |
|------|--------|----------------------------------------|-------|
| Tier 1 | ≥ $0.50 | ≥0.50→70% · ≥1.00→75% · ≥1.50→80% · ≥2.00→85% | $0.35 |
| Tier 2 | ≥ $0.80 | ≥0.80→70% · ≥2.00→75% · ≥3.00→80% · ≥4.00→85% | $0.50 |

Examples — **Tier 1:** peak $0.50→protect $0.35, $1.00→$0.75, $1.50→$1.20,
$2.00→$1.70. **Tier 2:** peak $0.80→$0.56, $2.00→$1.50, $3.00→$2.40, $4.00→$3.40.

It is **per-account** (never affects another account), **resets** once all
positions close and on the new UTC day, and is **independent of and compatible
with** the daily profit lock (which only blocks new entries). It does not block
new entries itself.

---

## Account rules

| Rule | Value |
|------|-------|
| Leverage (default / admin override / never exceed) | 10× / 8×–10× / 10× |
| Max positions | 8 (Tier 1) / 10 (Tier 2) |
| Positions per symbol | 1 (single entry) |
| Same-symbol cooldown after a close | 30 min (symbol-specific) |
| Daily profit target | stop new trades until next UTC day |
| Daily loss limit | close all positions immediately, lock until next UTC day |

### Account death protection (PROTECTION_LOCKED)

If account **equity** (wallet balance + open floating PnL) falls below the tier
floor — **$15 (Tier 1)** or **$30 (Tier 2)** — the account is **permanently**
PROTECTION_LOCKED: all positions are closed immediately and trading is disabled
until an admin manually resets it (`python main.py --clear-protection <ACCOUNT_ID>`).
The lock survives restarts and is **not** cleared by the UTC-day reset.

### Daily PnL & deposit/transfer protection

Daily profit and loss are computed **only** from trading activity — realised PnL
(closed positions) + unrealised PnL (open positions) — and **never** from
wallet-balance changes. Deposits, withdrawals, and transfers can never reset the
daily counters. The daily loss limit fires on this total (it does not wait for
losses to be realised) and immediately closes all positions.

---

## Multi-Account Isolation

Every account is fully independent. Daily profit/loss counters, lock state,
cooldowns, position state, and trading-permission state are **per-account** and
**never global** — one account hitting a limit only locks **that** account. All
risk state is keyed `account_<id>_…` in the `bot_state` table (via
`AccountDatabaseWrapper`); each account has its own `RiskManager`. Locks persist
across restart/crash and clear only on the next UTC-day reset (except the
permanent protection lock).

### Exchange safety & partial fills

- Before sending an order, sizing validates **minimum notional, minimum
  quantity, step size, and precision** — invalid orders are rejected with the
  exact reason.
- **Partial fills are never assumed to be full.** The position records the
  *actual* filled quantity and the *actual* margin consumed, and TP/SL are
  recomputed from those values. A partial close keeps closing the remainder
  until flat.

---

## Safety Guarantees

The system will **never**:

- Average down · open a Layer 2 · martingale · expand a grid
- Increase leverage automatically · scale position size by balance
- Trade an unsupported symbol · ignore the daily loss limit · ignore the BTC filter

---

## Architecture (preserved infrastructure)

The multi-account platform is unchanged — only trade generation/exit was rebuilt
to single entry.

```
main.py                  CLI entry point
├── core/engine.py       Main loop: BTC filter + 100-symbol signal fan-out + universe validation
├── config/settings.py   All strategy parameters + the fixed 100-symbol universe
├── core/database.py     PostgreSQL persistence (SQLAlchemy)
├── core/models.py       ORM models (users, accounts, subscriptions, positions, …)
├── accounts/            Multi-account manager + Fernet encryption
├── execution/executor.py  Per-account isolated signal fan-out
├── exchange/client.py   CCXT Binance USDT-M Futures (per-account credentials)
├── signals/             indicators · btc_regime · signal_engine
├── grid/                take_profit (TP/SL) · position_manager (single entry)
├── risk/                position_sizer · risk_manager
├── services/sync.py     Background balance/position/risk sync
├── control/bot_control.py  Runtime control plane
└── admin/               Admin REST API
```

> **Storage note:** a position is persisted as a *basket holding exactly one
> layer*. The `baskets` / `recovery_layers` tables and the `Basket` /
> `RecoveryLayer` DTOs are **retained for database-schema compatibility** (the
> schema is shared with the web platform); they now always represent a single
> entry. No destructive migration is required.

---

## Logging

| File | Content |
|------|---------|
| `logs/bot.log` | Main operations |
| `logs/trades.log` | OPEN / CLOSE / SL_HIT events · TP fast-path (`TP_DETECTED`, `TP_LOCK_ACTIVATED`, `TP_CLOSE_SENT`, `TP_CLOSE_CONFIRMED`) · `PORTFOLIO_PROFIT_LOCK` |
| `logs/execution.log` | Per-account execution |
| `logs/control.log` | Control-plane events |
| `logs/errors.log` | Errors |

---

## Quick Start

```bash
pip install -r requirements.txt

# Configure DATABASE_URL and MASTER_ENCRYPTION_KEY (see .env.example)
python scripts/generate_encryption_key.py   # → MASTER_ENCRYPTION_KEY

# Run live trading (testnet controlled by config/config.json → use_testnet)
python main.py --mode live --api
```

Accounts (encrypted API keys, subscriptions) are managed via the database /
website. The bot trades every eligible account with the same fixed strategy.

---

## Configuration

All parameters live in `config/config.json` (loaded by `config/settings.py`).
Key values:

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `supported_symbols` | 100 USDT-M perps | the only tradeable pairs |
| `timeframe` | `15m` | candle interval |
| `default_leverage` / `min` / `max` / `hard_max` | 10 / 8 / 10 / 10 | leverage policy |
| `min_tier_balance` | 20.0 | below this → no tier, no trading |
| `account_tiers` | tier1 / tier2 | per-tier margin, position cap, daily limits, death floor |
| `tp_margin_pct` | 0.20 | take-profit = 20% of margin (net) |
| `sl_margin_pct` | 0.12 | stop-loss = 12% of margin (net) |
| `account_tiers[].portfolio_lock_trigger` / `_floor` | T1 0.50/0.35 · T2 0.80/0.50 | arm trigger + protected floor |
| `account_tiers[].portfolio_protection_bands` | T1/T2 70→85% bands | dynamic trail: `protected = max(floor, peak × band%)` |
| `atr_entry_min_pct` / `_max_pct` | 0.003 / 0.012 | ATR feasibility band |
| `min_signal_score` | 1 | minimum signal-strength score (0–4) |
| `max_basket_per_symbol` | 1 | never two positions on one symbol |
| `symbol_cooldown_seconds` | 1800 | 30-min same-symbol cooldown after a close |
| `taker_fee_pct` | 0.0005 | realistic Binance taker fee (0.05%) |

Per-tier values (`margin_per_trade`, `daily_profit_target`, `daily_loss_limit`,
`max_active_symbols`, `max_positions`, `protection_floor`) live inside
`account_tiers` — see the table above.

---

## Disclaimer

⚠️ **For educational and research purposes only.** Trading cryptocurrencies
involves substantial risk of loss. Test thoroughly on testnet before using real
funds. **USE AT YOUR OWN RISK.**
