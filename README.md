# Zentry Futures Core

A standalone hybrid Binance USDT-M Futures scalping grid bot optimised for small accounts (30–500 USDT). Focuses on **profitability**, **capital preservation**, and **low drawdown**.

---

## Features

- **Hybrid Strategy** — Signal-based entries with controlled recovery averaging and basket take-profit
- **Small Account Optimised** — Works with as little as 30 USDT, automatically scales sizing
- **Dynamic Coin Scanner** — Scans all USDT-M futures pairs every 10 minutes, scores by volume, ATR, spread, and funding rate
- **Market Classification** — Detects trending/sideways regimes and low/medium/high volatility to adapt parameters
- **2-Layer Recovery** — Initial entry + ONE recovery layer (NOT martingale) with ATR-based spacing; bounds losing-basket size
- **Three-Tier Stop Loss** — Individual (3× ATR), basket (15% margin), emergency (3% account)
- **Basket Take Profit** — Primary exit: fixed 15% ROI target with a trailing profit-protection lock
- **Risk Management** — Tiered daily drawdown, 25%+ max exposure, catastrophic-drawdown shutdown
- **Daily Profit Trailing Lock** — Ratcheting floor (8→5, 10→8, 12→10) + hard stop at 15% daily gain
- **Loss-Streak Pause** — 3 consecutive losing baskets pauses new entries for 1 hour (auto-expiring, persisted)
- **Dynamic Leverage** — 10× low vol, 8× medium, 5× high volatility
- **Complete Backtesting** — Bar-by-bar simulation with slippage, fees, and all risk rules
- **Docker Deployment** — Production-ready with volume persistence

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12 |
| Exchange API | CCXT (Binance USDT-M Futures) |
| Database | SQLite (WAL mode) |
| Deployment | Docker / Docker Compose |

---

## Quick Start

### Prerequisites

- Python 3.12+
- Binance Futures account (Testnet or Live)
- API key and secret

### Installation

```bash
# Clone or download the project
cd botfinal

# Install dependencies
pip install -r requirements.txt

# Configure API keys
# Edit config/config.json and add your API key and secret
```

### Configuration

Edit `config/config.json`:

```json
{
    "api_key": "YOUR_BINANCE_API_KEY",
    "api_secret": "YOUR_BINANCE_API_SECRET",
    "use_testnet": true
}
```

> **Important:** Leave `use_testnet` as `true` until you've tested thoroughly on testnet.

### Run Live Trading

```bash
# Testnet (default)
python main.py --mode live

# With custom config path
python main.py --mode live --config path/to/config.json
```

### Run Backtesting

```bash
# Single symbol
python main.py --mode backtest \
    --symbols BTC/USDT:USDT \
    --start 2026-01-01 \
    --end 2026-03-01 \
    --balance 100

# Multiple symbols
python main.py --mode backtest \
    --symbols BTC/USDT:USDT ETH/USDT:USDT SOL/USDT:USDT \
    --start 2026-01-01 \
    --end 2026-04-01 \
    --balance 50
```

---

## Docker Deployment

```bash
# Build and start
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down

# Rebuild after code changes
docker-compose up -d --build
```

Data, logs, and config are persisted via volume mounts.

---

## Configuration Guide

### Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `use_testnet` | `true` | Connect to Binance Testnet |
| `scan_interval_seconds` | `600` | Coin scan interval (10 min) |
| `min_volume_24h` | `50000000` | Minimum 24h volume filter ($50M) |
| `max_watchlist_size` | `20` | Maximum pairs in watchlist |
| `daily_loss_limit_pct` | `0.05` | 5% daily loss limit |
| `max_exposure_pct` | `0.25` | 25% max capital exposure |
| `max_drawdown_pct` | `0.15` | 15% max drawdown (triggers shutdown) |
| `recovery_max_layers` | `2` | Maximum layers per basket (initial entry + 1 recovery) |

### Balance-Tier Basket Sizing (fixed, not % of balance)

| Tier | Account Size | Layer 1 | Layer 2 | Max Basket Margin | Max Positions |
|------|-------------|---------|---------|-------------------|---------------|
| A | $10 – $50 | $1.50 | $1.00 | $2.50 | 8 |
| B | $50 – $200 | $2.50 | $1.00 | $3.50 | 8 |
| C | > $200 | $3.50 | $1.00 | $4.50 | 8 |

First tier whose `max_balance >= balance` wins ($50 → A, $200 → B). The two layers
sum to exactly the tier's max-basket cap, which a basket can never exceed.

### Leverage by Volatility

| Volatility | Leverage |
|-----------|----------|
| Low | 10× |
| Medium | 8× |
| High | 5× |

---

## Risk Management

### Safety Mechanisms

1. **Daily Loss Limit (5%)** — Closes all positions and pauses trading until the next UTC day
2. **Max Exposure (25%)** — Blocks new entries when total margin exposure exceeds 25% of balance
3. **Max Drawdown (15%)** — Emergency shutdown with manual restart required
4. **Emergency Stop Loss** — Force-closes any basket whose loss exceeds 3% of total account
5. **Basket Stop Loss** — Closes basket when loss exceeds 15% of basket margin
6. **Individual Stop Loss** — Per-layer stop at 3× ATR from entry
7. **Daily Profit Trailing Lock** — Once daily gain reaches 8/10/12%, arms a 5/8/10% floor; if gain falls back to the floor, new entries stop for the day. 15% gain is an immediate hard stop. Existing positions still managed.
8. **Loss-Streak Pause** — 3 consecutive losing baskets pauses new entries for 1 hour (per-account, persisted, auto-expiring)

### Emergency Shutdown

If the max drawdown limit is hit, the bot will:
1. Close all positions
2. Set an emergency shutdown flag in the database
3. Refuse to restart until manually cleared

To clear:
```bash
python main.py --clear-shutdown
```

---

## Architecture

```
main.py                  CLI entry point
├── core/engine.py       Main trading loop orchestrator
├── config/settings.py   Configuration & enums
├── core/database.py     SQLite persistence
├── exchange/client.py   CCXT Binance Futures wrapper
├── scanner/             Coin scoring & watchlist
├── signals/             RSI + EMA200 signal engine
├── grid/                Position manager, recovery, TP
├── risk/                Risk manager, position sizer, SL
└── backtest/            Backtesting engine & reporting
```

### Entry Signal Logic

- **LONG:** Price > EMA200 (1h) AND RSI(14) < 30 (5m)
- **SHORT:** Price < EMA200 (1h) AND RSI(14) > 70 (5m)

### Recovery System (2 layers max)

```
Layer 1 (initial entry): tier['layer1']    at entry
Layer 2 (one recovery):  tier['layer2']     at 0.75 × ATR from Layer 1
```

Per-tier margins: A $1.50/$1.00, B $2.50/$1.00, C $3.50/$1.00. There is no third
or fourth layer — the maximum number of layers per basket is 2.

---

## Logs

| File | Content |
|------|---------|
| `logs/bot.log` | Main operations (INFO) |
| `logs/trades.log` | All entry/exit events |
| `logs/errors.log` | Errors and exceptions |

All log files rotate at 5 MB with 10 backups.

---

## Disclaimer

⚠️ **This software is provided for educational and research purposes only.**

- Trading cryptocurrencies involves substantial risk of loss
- Past performance does not guarantee future results
- The authors are not responsible for any financial losses
- Always test thoroughly on testnet before using real funds
- Never invest more than you can afford to lose

**USE AT YOUR OWN RISK.**
