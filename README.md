# Zentry Futures Core

A standalone hybrid Binance USDT-M Futures scalping grid bot optimised for small accounts (30–500 USDT). Focuses on **profitability**, **capital preservation**, and **low drawdown**.

---

## Features

- **Hybrid Strategy** — Signal-based entries with controlled recovery averaging and basket take-profit
- **Small Account Optimised** — Works with as little as 30 USDT, automatically scales sizing
- **Dynamic Coin Scanner** — Scans all USDT-M futures pairs every 10 minutes, scores by volume, ATR, spread, and funding rate
- **Market Classification** — Detects trending/sideways regimes and low/medium/high volatility to adapt parameters
- **4-Layer Recovery** — Controlled averaging (NOT martingale) with ATR-based spacing and gentle margin progression
- **Three-Tier Stop Loss** — Individual (3× ATR), basket (20% margin), emergency (3% account)
- **Basket Take Profit** — Primary exit: 8/12/15% ROI targets by volatility across all layers
- **Risk Management** — 5% daily loss limit, 25% max exposure, 15% max drawdown with emergency shutdown
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
| `recovery_max_layers` | `4` | Maximum recovery layers per basket |

### Position Sizing Tiers

| Account Size | Margin per Layer 1 | Max Positions |
|-------------|-------------------|---------------|
| < 50 USDT | 0.40 – 0.60 USDT | 3 |
| 50 – 100 USDT | 0.60 – 0.80 USDT | 5 |
| > 100 USDT | 0.90 – 1.50 USDT | 8 |

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
5. **Basket Stop Loss** — Closes basket when loss exceeds 20% of basket margin
6. **Individual Stop Loss** — Per-layer stop at 3× ATR from entry

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

### Recovery System

```
Layer 1: base_margin × 1.00    at entry
Layer 2: base_margin × 1.33    at 0.75 × ATR from Layer 1
Layer 3: base_margin × 1.67    at 1.75 × ATR from Layer 1 (cumulative)
Layer 4: base_margin × 2.17    at 3.00 × ATR from Layer 1 (cumulative)
```

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
