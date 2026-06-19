# ZenGrid Futures Core — Dark-Venus Basket Recovery

A lightweight, survival-first **basket recovery** trading core for Binance
USDT-M Futures, inspired by the Dark Venus model. It trades only three liquid,
low-priced pairs on the 15-minute timeframe using mean-reversion entries, a
controlled 2-layer recovery basket, and fixed-dollar basket take-profit.

The goal is **not** maximum profit. The priority order is:

1. **Survival**
2. **Drawdown control**
3. **Consistency**
4. **Profit**

Profit is never prioritised over survival.

---

## Supported Symbols (ONLY these)

- `TRXUSDT`
- `XRPUSDT`
- `XLMUSDT`

No other symbols are ever traded. Timeframe is **15-minute candles only**.

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
- **Layer 2:** the single recovery layer (2× Layer-1 margin), activated only when
  Layer-1 drawdown ≥ `ATR(14) × 2`. Spacing is volatility-adjusted (ATR-based),
  never fixed grid spacing.

When the recovery layer activates the basket take-profit target is recalculated.
The **entire basket** is closed together when its net profit (after fees) reaches
the tier target.

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
| Daily profit target | $3 | $4 |
| Daily loss limit | $3 | $4 |

Balances below $20 have no tier and do not trade.

### Account rules

| Rule | Value |
|------|-------|
| Leverage (default / admin override / never exceed) | 5× / 3×–8× / 10× |
| Max active symbols / baskets per account | 2 |
| Max basket per symbol | 1 |
| Max layers per basket | 2 |
| Max total open positions | 4 (2 baskets × 2 layers) |
| Same-symbol cooldown after a close | 15 min |
| Daily profit target | stop new trades until next UTC day |
| Daily loss limit | close all baskets immediately, lock until next UTC day |

The only honoured per-account override is the admin leverage setting. All sizing,
exposure caps, TP targets, and daily limits are tier-fixed.

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
| `default_leverage` / `min` / `max` / `hard_max` | 5 / 3 / 8 / 10 | leverage policy |
| `min_tier_balance` | 20.0 | below this → no tier, no trading |
| `account_tiers` | tier1 / tier2 | per-tier margins, exposure caps, TP targets, daily limits |
| `layer2_atr_multiplier` | 2.0 | Layer-2 distance = ATR×2 |
| `max_baskets_per_account` | 2 | max simultaneous baskets |
| `max_total_open_positions` | 4 | 2 baskets × 2 layers |
| `symbol_cooldown_seconds` | 900 | 15-min same-symbol cooldown after a close |

Per-tier values (`layer1_margin`, `layer2_margin`, `max_basket_exposure`,
`basket_tp_l1`, `basket_tp_l2`, `daily_profit_target`, `daily_loss_limit`) live
inside `account_tiers` — see the Account Tier System table above.

---

## Disclaimer

⚠️ **For educational and research purposes only.** Trading cryptocurrencies
involves substantial risk of loss. Test thoroughly on testnet before using real
funds. **USE AT YOUR OWN RISK.**
