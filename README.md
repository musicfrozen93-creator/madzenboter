# ZenGrid Futures Core — Dark-Venus Basket Recovery

A lightweight, survival-first **basket recovery** trading core for Binance
USDT-M Futures, inspired by the Dark Venus model. It trades a curated list of
10 liquid, correlated USDT-M pairs on the 15-minute timeframe using mean-reversion
entries, a controlled 2-layer recovery basket, and dollar + ROI basket take-profit.

The goal is **not** maximum profit. The priority order is:

1. **Survival**
2. **Drawdown control**
3. **Consistency**
4. **Profit**

Profit is never prioritised over survival.

---

## Supported Symbols (watchlist — ONLY these are traded)

`TRXUSDT`, `XRPUSDT`, `XLMUSDT`, `ADAUSDT`, `ALGOUSDT`, `HBARUSDT`, `VETUSDT`,
`LINKUSDT`, `DOTUSDT`, `ATOMUSDT` — 10 correlated pairs.

No other symbols are ever traded. Timeframe is **15-minute candles only**.
Expanding the watchlist only adds candidate setups — tier sizing, exposure caps,
layer count, and per-account position limits still bound all risk.

---

## Strategy

**Trading style:** mean reversion · recovery basket · basket TP closure ·
controlled recovery (not a traditional grid, never a martingale).

### BTC trend filter (runs before every new basket)

Uses `BTCUSDT` 15m:

| BTC state | Condition | Effect |
|-----------|-----------|--------|
| Bullish | price > EMA200 **and** EMA50 > EMA200 | block SHORT |
| Bearish | price < EMA200 **and** EMA50 < EMA200 | block LONG |
| Neutral | otherwise | allow both |

### Entry logic (all conditions must be true)

- **LONG:** RSI(14) < 30 **and** price touches the lower Bollinger band **and** BTC filter approves.
- **SHORT:** RSI(14) > 70 **and** price touches the upper Bollinger band **and** BTC filter approves.

### Pre-trade risk filters (skip the trade if any trips)

- Spread too high
- ATR explosion (current ATR > 2.5× average ATR)
- News / oversized candle (candle body > 2.5× ATR)
- Volume spike (> 3× average volume)

### Recovery model (max 2 layers — NO Layer 3/4/5)

- **Layer 1:** fixed tier margin, opened on the entry signal.
- **Layer 2:** the single recovery layer (2× Layer-1 margin), activated on a
  **hybrid trigger** — whichever occurs first of: Layer-1 drawdown ≥ `ATR(14) × 2`
  (`ATR_TRIGGER`), or Layer-1 floating loss ≥ `$0.50` (`LOSS_TRIGGER`). ATR
  spacing is volatility-adjusted, never fixed grid spacing.

The **entire basket** closes together on the **first** of two conditions (both
net of fees), for **every** basket:

1. **Fixed-USD target** — Layer 1 only $0.50/$0.80, recovery $1.50/$2.00.
2. **ROI target** — `net PnL / total margin ≥ tier ROI` (Tier 1 12%, Tier 2 10%):
   - Layer-1-only → $0.24 (T1) / $0.40 (T2), logs `ROI_L1_EXIT`
   - Recovery → $0.60 (T1) / $1.20 (T2), logs `ROI_RECOVERY_EXIT`

The ROI dollar amount sits below the matching USD target, so a profitable basket
closes earlier (frees capital, faster profit realisation, improves turnover)
instead of waiting for the larger fixed-USD target.

---

## Account Tier System

Balance is evaluated **only** to select one of exactly two tiers. The tier is
**locked onto the basket at open** — recovery margin, exposure cap, and TP target
never change afterward, even if the balance later crosses a boundary (e.g. a
deposit). Within a tier, sizing is fixed: no balance scaling, no percentage
sizing, no dynamic/adaptive/volatility sizing, no martingale.

| Setting | Tier 1 ($20–$39.99) | Tier 2 ($40+) |
|---------|---------------------|----------------|
| Layer 1 margin | $2 | $4 |
| Layer 2 margin | $4 | $8 |
| Max basket exposure | $6 | $12 |
| Basket TP (Layer 1) | $0.50 | $0.80 |
| Basket TP (Layer 1 + 2) | $1.50 | $2.00 |
| Layer-1 ROI target | 12% (→ $0.24) | 10% (→ $0.40) |
| Recovery ROI target (≥2 layers) | 10% (→ $0.60) | 10% (→ $1.20) |
| Daily profit target | $3 | $4 |
| Daily loss limit | $3 | $4 |
| Max active symbols | 2 | 3 |
| Max positions | 4 | 6 |
| Death-protection floor (equity) | $15 | $30 |

Balances below $20 have no tier and do not trade.

### Account rules

| Rule | Value |
|------|-------|
| Leverage (default / admin override / never exceed) | 8× / 5×–10× / 10× |
| Max active symbols | 2 (Tier 1) / 3 (Tier 2) |
| Max positions | 4 (Tier 1) / 6 (Tier 2) |
| Max basket per symbol | 1 |
| Max layers per basket | 2 |
| Same-symbol cooldown after a close | 15 min |
| Daily profit target | stop new trades until next UTC day |
| Daily loss limit | close all baskets immediately, lock until next UTC day |

Leverage is fixed (never dynamically adjusted, never balance-scaled). The only
honoured per-account override is the admin leverage setting. All sizing, exposure
caps, TP targets, daily limits, and position limits are tier-fixed.

### Account death protection (PROTECTION_LOCKED)

If account **equity** (wallet balance + open floating PnL) falls below the tier
floor — **$15 (Tier 1)** or **$30 (Tier 2)** — the account is **permanently**
PROTECTION_LOCKED: all baskets are closed immediately and trading is disabled
until an admin manually resets it. The lock is stored in the database and
survives bot/server restart and crashes (it is **not** cleared by the UTC-day
reset). Admin reset: `python main.py --clear-protection <ACCOUNT_ID>`.

### Correlation protection

TRX/XRP/XLM are treated as **correlated** assets. Each signal gets a
**strength score (0–4)**: +1 extreme RSI (<20 / >80), +1 strong Bollinger
penetration, +1 BTC trend strongly aligned, +1 good spread & liquidity. A new
basket needs a higher score the more correlated baskets are already open —
**0 open → score ≥ 2**, **1+ open → score ≥ 3** — and is rejected once the tier's
max active symbols is reached.

### Daily PnL & deposit/transfer protection

Daily profit and loss are computed **only** from trading activity —
**realised PnL (closed baskets) + unrealised PnL (open baskets)** — and **never**
from wallet-balance changes. Deposits, withdrawals, funding/spot transfers,
internal Binance transfers, and manual adjustments are ignored and can never
reset the daily profit/loss counters. The daily loss limit fires on this total
(it does **not** wait for losses to be realised) and immediately closes all
baskets and recovery layers.

---

## Multi-Account Isolation

Every account is fully independent. Daily profit/loss counters, daily lock state,
cooldowns, basket/recovery state, and trading-permission state are **per-account**
and **never global** — one account hitting a limit only locks **that** account.

- All risk state is keyed `account_<id>_…` in the `bot_state` table and queried
  account-scoped (via `AccountDatabaseWrapper`); each account has its own
  `RiskManager`. The bot is never globally stopped by a per-account limit.
- **Lock persistence:** daily profit/loss locks live in the database, so a bot
  restart, server restart, or crash never removes them. Locks clear only on the
  next UTC-day reset, which also resets the daily profit/loss counters.

### Entry validation order

Before every new trade, in this order (BTC filter + signal validity already ran
upstream in the signal engine): **1.** account lock status · **2.** daily profit
limit · **3.** daily loss limit · **4.** cooldown · then structural limits and
exchange-safety sizing · then execute.

### Exchange safety & partial fills

- Before sending an order, sizing validates **minimum notional, minimum quantity,
  step size, and precision** — invalid orders are rejected with the exact reason.
- **Partial fills are never assumed to be full.** The basket records the *actual*
  filled quantity and the *actual* margin consumed, and basket TP and exposure
  are recomputed from those actual values.

---

## Safety Guarantees

The system will **never**:

- Martingale infinitely · open Layer 3+ · increase leverage automatically
- Scale position size by balance · trade unsupported symbols
- Ignore the daily loss limit · ignore the BTC trend filter

---

## Architecture (preserved infrastructure)

The multi-account platform is unchanged — only trade generation/execution was
rebuilt.

```
main.py                  CLI entry point
├── core/engine.py       Main loop: BTC filter + 3-symbol signal fan-out
├── config/settings.py   All strategy parameters + enums
├── core/database.py     PostgreSQL persistence (SQLAlchemy)
├── core/models.py       ORM models (users, accounts, subscriptions, baskets, …)
├── accounts/            Multi-account manager + Fernet encryption
├── execution/executor.py  Per-account isolated signal fan-out
├── exchange/client.py   CCXT Binance USDT-M Futures (per-account credentials)
├── signals/             indicators · btc_regime · signal_engine
├── grid/                recovery · take_profit · position_manager
├── risk/                position_sizer · risk_manager
├── services/sync.py     Background balance/position/risk sync
├── control/bot_control.py  Runtime control plane
└── admin/               Admin REST API
```

Kept from the original platform: multi-account architecture, account isolation,
account-specific API execution, database structure, encryption, subscriptions,
user management, trade history, logging, and the risk-tracking framework.

---

## Logging

Every entry / skip / recovery / close records the **account id, symbol,
direction, entry price, recovery layer, and reason**.

| File | Content |
|------|---------|
| `logs/bot.log` | Main operations |
| `logs/trades.log` | OPEN / RECOVERY / CLOSE events |
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
| `supported_symbols` | TRX/XRP/XLM | the only tradeable pairs |
| `timeframe` | `15m` | candle interval |
| `default_leverage` / `min` / `max` / `hard_max` | 8 / 5 / 10 / 10 | leverage policy |
| `min_tier_balance` | 20.0 | below this → no tier, no trading |
| `account_tiers` | tier1 / tier2 | per-tier sizing, caps, TP, daily limits, position limits, death floor |
| `layer2_atr_multiplier` | 2.0 | Layer-2 distance = ATR×2 |
| `correlation_min_score_first` / `_additional` | 2 / 3 | min signal score for 0 / 1+ active baskets |
| `max_basket_per_symbol` | 1 | never two baskets on one symbol |
| `symbol_cooldown_seconds` | 900 | 15-min same-symbol cooldown after a close |

Per-tier values (`layer1_margin`, `layer2_margin`, `max_basket_exposure`,
`basket_tp_l1`, `basket_tp_l2`, `daily_profit_target`, `daily_loss_limit`,
`max_active_symbols`, `max_positions`, `protection_floor`) live inside
`account_tiers` — see the Account Tier System table above.

---

## Disclaimer

⚠️ **For educational and research purposes only.** Trading cryptocurrencies
involves substantial risk of loss. Test thoroughly on testnet before using real
funds. **USE AT YOUR OWN RISK.**
