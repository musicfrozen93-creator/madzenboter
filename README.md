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

| Layer | Margin | Trigger | Basket target |
|-------|--------|---------|---------------|
| 1 | fixed | entry signal | ≈ $0.50 net |
| 2 | 2× Layer 1 | Layer-1 drawdown ≥ `ATR(14) × 2` | ≈ $1.50–$2.00 net |

Spacing is volatility-adjusted (ATR-based), never fixed grid spacing. When the
recovery layer activates, the basket take-profit target is recalculated. The
**entire basket** is closed together when its net profit reaches the target.

---

## Account Rules

Every account uses **exactly the same** fixed sizing model. Account balance does
**not** affect margin size, position count, recovery size, or layer count.

| Rule | Value |
|------|-------|
| Leverage (default) | 5× |
| Leverage (admin override) | 3× – 8× |
| Leverage (never exceed) | 10× |
| Layer 1 margin | fixed (`layer1_margin_usd`) |
| Layer 2 margin | 2 × Layer 1 |
| Max active symbols / baskets per account | 2 |
| Max basket per symbol | 1 |
| Daily profit target (per account) | $5 → stop new trades until next UTC day |
| Daily loss limit (per account) | $3 → close all baskets, stop until next UTC day |

The only honoured per-account override is the admin leverage setting. All sizing,
limits, and targets are globally fixed.

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
| `layer1_margin_usd` | 5.0 | fixed Layer-1 margin |
| `layer2_margin_multiplier` | 2.0 | Layer 2 = 2× Layer 1 |
| `layer2_atr_multiplier` | 2.0 | Layer-2 distance = ATR×2 |
| `basket_tp_layer1_usd` | 0.50 | basket TP (Layer 1 only) |
| `basket_tp_recovery_usd` | 1.75 | basket TP (after recovery) |
| `daily_profit_target_usd` | 5.0 | stop new entries when reached |
| `daily_loss_limit_usd` | 3.0 | close all + stop when reached |
| `max_baskets_per_account` | 2 | max simultaneous baskets |

---

## Disclaimer

⚠️ **For educational and research purposes only.** Trading cryptocurrencies
involves substantial risk of loss. Test thoroughly on testnet before using real
funds. **USE AT YOUR OWN RISK.**
