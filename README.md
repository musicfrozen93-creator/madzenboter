# Zentry — Market Analysis API

The Python backend of the **Zentry AI Trading Signal Platform**. A FastAPI
service that analyses a market on request and returns one professional trading
signal.

> **This service never places a trade.** It holds no exchange API keys, cannot
> reach a private venue endpoint, has no background loop, and exposes no order,
> balance, or position route. `GET /health` reports `trading_enabled: false`, and
> the test suite asserts that no trading endpoint exists.

---

## Architecture

```
Next.js Frontend          auth · dashboard · UI · displaying signals
      ↓ REST
Market Analysis API       ← this service
      ↓
Market Data Provider      standardized candles / quotes / symbols
      ↓
Multi-Timeframe Engine    3 timeframes analysed, 1 reported
      ↓
Analysis Engine           indicators · regime · structure · S/R · fib · waves
      ↓
Confluence Engine         8 module votes, aggregated, conflicts named
      ↓
Signal Generator          direction · entry · SL · TP1/TP2/TP3
      ↓
Quality + Confidence      two deterministic 0–100 scores
      ↓
Explanation Engine        plain-English reasons
      ↓
JSON Response
      ↓
Dashboard
```

The frontend contains **no** business logic. Every technical calculation lives
here and is the single source of truth — nothing is reimplemented in JavaScript.

The Analysis Engine never imports an exchange client. It consumes the
`MarketDataProvider` interface, so a new market is added by writing one provider
class, not by touching analysis code. See [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Layout

| Package | Responsibility |
|---------|----------------|
| `api/` | Transport only: FastAPI app, routes, request/response schemas, serializers |
| `providers/` | **Market Data Provider layer** — the venue abstraction (`base`, `registry`, `binance`) |
| `analysis/` | The pipeline — see below |
| `signals/` | Indicator primitives: RSI/EMA/SMA/ATR/Bollinger, benchmark regime, market-quality filters, mean-reversion detector |
| `v4/` | ADX/DI, regime detection, stop math, trend/breakout setup engines |
| `core/` | ORM models (`users`, `subscriptions`, `signals`), database, the `Signal` DTO |
| `config/` | Analysis parameters |

Inside `analysis/`:

| Module | Responsibility |
|--------|----------------|
| `timeframes.py` | Multi-timeframe ladder — analyses 3 timeframes per request |
| `engine.py` | One timeframe → indicators, regime, structure, S/R, Fibonacci, waves |
| `structure.py` | Swing detection, HH/HL trend, Break of Structure / Change of Character |
| `levels.py` | Support and resistance zone clustering and strength |
| `fibonacci.py` | Retracements, golden pocket, extensions (target candidates) |
| `elliott.py` | Rule-based wave counting with a genuine alternative count |
| `smc/` | Smart Money confirmations — order blocks, FVG, liquidity, VWAP, MACD, ADX, patterns |
| `intelligence/` | Trade-intelligence layer — validation, health, guide, lifecycle, risk, invalidation, explanations (read-only review; never alters the signal) |
| `modules.py` | The fifteen module votes and the single weight table |
| `confluence.py` | Vote aggregation, conflict detection |
| `generator.py` | Direction, entry, stop, TP1/TP2/TP3 — all derived from analysis |
| `scoring.py` | Quality Score and Confidence Score, deterministic and explainable |
| `explanation.py` | Analysis → plain English reasons (not AI) |
| `cache.py` | TTL candle cache + per-request memo |

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # set DATABASE_URL; there is no API key to configure
python main.py
```

The API listens on `http://localhost:8000`, with interactive docs at `/docs`.

```bash
python main.py --reload     # development, auto-reload
python main.py --check      # validate config and providers, then exit
python main.py --init-db    # create database tables, then exit
python main.py --port 9000
pytest                      # 211 tests, no network required
```

Docker:

```bash
docker compose up -d
```

That starts `postgres` and `analysis-api`. Point the website's
`ANALYSIS_API_URL` at the published API port.

---

## API

### `POST /api/analyze`

```json
{ "market": "crypto", "symbol": "BTCUSDT", "timeframe": "15m" }
```

The timeframe you send is the one you trade. The engine internally analyses two
higher timeframes for trend and structure confirmation and never returns them.

```json
{
  "signal": "BUY",
  "market": "crypto", "provider": "binance",
  "symbol": "AVAXUSDT", "timeframe": "15m",
  "quality": 64, "quality_grade": "Weak",
  "confidence": 58, "confidence_grade": "Moderate",
  "entry": 6.442,
  "sl": 6.378,
  "tp": [6.504, 6.524, 6.587],
  "risk_reward": 2.27,
  "risk_pct": 0.0099,
  "entry_basis": "current market price",
  "stop_basis": "0.5 ATR beyond the protective swing at 6.40400000 (1.00% risk)",
  "target_sources": ["resistance zone (1 touches)", "...", "..."],
  "headline": "BUY AVAXUSDT on 15m — Weak setup (64/100), moderate confidence (58/100)",
  "reasons": ["✓ Higher timeframe trend bullish …", "✕ …", "• …"],
  "analysis": {
    "breakdown": [{"module": "trend", "label": "Bullish (HTF confirmed)", "score": 9.0, "max_score": 20.0}, "…"],
    "trend": "bullish",
    "structure": {"trend": "bullish", "event": "bos", "…": "…"},
    "elliott": {"current_wave": "Wave C", "confidence": 51.0, "alternative": {"label": "Wave 3", "confidence": 49.0}},
    "fibonacci": {"in_golden_pocket": false, "…": "…"},
    "levels": {"position": "just above support", "…": "…"},
    "confluence": {"agreement": 0.72, "conflicts": []},
    "timeframes_analyzed": 3
  },
  "quality_detail": {"value": 64, "grade": "Weak", "components": ["…"]},
  "confidence_detail": {"value": 58, "grade": "Moderate", "components": ["…"]},
  "generated_at": "2026-08-07T12:00:00+00:00",
  "elapsed_ms": 712
}
```

`WAIT` is a **200**, not an error — it means no qualifying setup exists right
now, and `wait_reason` says why. Scores are still computed and returned. Errors
are reserved for real failures:

| Status | Meaning |
|--------|---------|
| 400 | Malformed/unlisted symbol, unknown timeframe, unknown market or provider |
| 422 | Valid request, but not enough candle history to analyse |
| 503 | The venue was unreachable |

Every value is calculated. There are no placeholder or nullable score fields.

### `GET /api/markets`
Registered markets and providers with their timeframes. No network access.

### `GET /api/markets/{market}/symbols`
Every symbol a market offers, for populating the pair selector.

### `GET /health`
Liveness, plus the `trading_enabled: false` guarantee.

---

## Adding a market

1. Subclass `MarketDataProvider` in `providers/`.
2. Register it in `providers/registry.py::_install_builtin_providers`.

Nothing in `analysis/` or `api/` changes. Full walkthrough in
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## Database

`users`, `subscriptions`, and `signals` come from `core/models.py`. The website
shares this database and layers its own tables on top via
`zentry-web/lib/db/migration_shared.sql`.

Tables left behind by the removed trading engine are **not** dropped by this
project — they are listed in `PHASE0_CLEANUP_REPORT.md` for deliberate review.

---

## Disclaimer

⚠️ **For educational and research purposes only.** Signals are analysis output,
not financial advice. Trading cryptocurrencies involves substantial risk of
loss. **USE AT YOUR OWN RISK.**
