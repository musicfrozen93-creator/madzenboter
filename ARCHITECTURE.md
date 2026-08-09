# Architecture

The mandatory structure of the Zentry AI Trading Signal Platform, and the rules
that keep it modular.

---

## 1. The layering

```
┌─────────────────────────────────────────────────────────────┐
│  Next.js Frontend        zentry-web                         │
│  auth · dashboard · UI · displaying signals                 │
│  NO business logic. NO indicator maths. NO scoring.         │
└─────────────────────────┬───────────────────────────────────┘
                          │  REST  (POST /api/analyze)
┌─────────────────────────▼───────────────────────────────────┐
│  API layer               api/                               │
│  validate · resolve provider · run pipeline · serialize     │
└─────────────────────────┬───────────────────────────────────┘
┌─────────────────────────▼───────────────────────────────────┐
│  Market Data Provider    providers/          ★ MANDATORY    │
│  standardized candles · quotes · symbols · timeframes       │
└─────────────────────────┬───────────────────────────────────┘
┌─────────────────────────▼───────────────────────────────────┐
│  Multi-Timeframe Engine  analysis/timeframes.py             │
│  3 rungs analysed (trend / structure / entry), 1 reported   │
└─────────────────────────┬───────────────────────────────────┘
┌─────────────────────────▼───────────────────────────────────┐
│  Analysis Engine         analysis/engine.py                 │
│  indicators · regime · structure · S/R · fib · waves        │
└─────────────────────────┬───────────────────────────────────┘
┌─────────────────────────▼───────────────────────────────────┐
│  Confluence Engine       analysis/confluence.py             │
│  8 module votes aggregated · conflicts named · WAIT is valid│
└─────────────────────────┬───────────────────────────────────┘
┌─────────────────────────▼───────────────────────────────────┐
│  Signal Generator        analysis/generator.py              │
│  BUY/SELL/WAIT · entry · SL · TP1/TP2/TP3 from real levels  │
└─────────────────────────┬───────────────────────────────────┘
┌─────────────────────────▼───────────────────────────────────┐
│  Quality + Confidence    analysis/scoring.py                │
│  two deterministic, fully explainable 0–100 scores          │
└─────────────────────────┬───────────────────────────────────┘
┌─────────────────────────▼───────────────────────────────────┐
│  Explanation Engine      analysis/explanation.py            │
│  analysis results → plain English (templates, not AI)       │
└─────────────────────────┬───────────────────────────────────┘
                          │  JSON
                          ▼
                      Dashboard
```

Computation primitives sit beneath the pipeline and are shared by every stage:

| Module | Provides |
|--------|----------|
| `signals/indicators.py` | RSI, EMA, SMA, ATR, Bollinger Bands |
| `signals/filters.py` | Market-quality checks (spread, ATR explosion, news candle, volume spike) |
| `signals/btc_regime.py` | Benchmark trend classification |
| `v4/indicators.py` | ADX / DI (Wilder) |
| `v4/regime.py` | Regime detection and engine routing |
| `v4/trade_math.py` | Stop placement (buffer, noise floor, risk ceiling) |
| `v4/engines/` | Trend and breakout setup engines |
| `analysis/structure.py` | Confirmed swings, HH/HL trend, BOS / CHoCH |
| `analysis/levels.py` | Support and resistance zone clustering |
| `analysis/fibonacci.py` | Retracements, golden pocket, extensions |
| `analysis/elliott.py` | Rule-based wave counting with alternatives |

---

## 2. Rules

**R1 — All analysis lives in Python.**
The frontend never computes an indicator, a score, or a price level. It renders
what `/api/analyze` returns. A calculation implemented twice in two languages is
two sources of truth, and they will drift.

**R2 — The Analysis Engine never imports an exchange client.**
It depends only on `providers.MarketDataProvider`. `grep -r "exchange.client"
analysis/` must return nothing. Only `providers/binance.py` may import it.

**R3 — Providers are read-only.**
A provider exposes candles, quotes, and symbol metadata. It must never accept
trading credentials or expose an order, balance, or position method.

**R4 — Symbols and timeframes are normalized at the edge.**
Callers use platform symbols (`BTCUSDT`) and canonical timeframes (`15m`). Each
provider translates to its own venue form internally. No venue-specific symbol
ever escapes the provider layer.

**R5 — Communication is REST.**
The website reaches the backend only through `lib/analysis-api.js` and the
authenticated `app/api/analyze/route.js` proxy. `ANALYSIS_API_URL` is
server-side only, so the analysis service is never exposed to the browser.

**R6 — WAIT is a real answer.**
No setup is a successful 200 with a `wait_reason`. The pipeline never invents a
direction or a price level to fill the response shape.

**R7 — Every returned value is calculated.**
No placeholder, no fixed multiplier, no randomness, no hardcoded output. Quality
and Confidence are deterministic sums over stated rules and return their full
component breakdown; take-profit levels come from real support/resistance zones,
Fibonacci extensions, and the structural measured move — never from an R
multiple. `test_no_placeholder_values_remain` enforces this at the API boundary.

**R8 — The user sees only the timeframe they selected.**
Higher timeframes are analysed for confirmation and must never appear in the
response. `test_internal_timeframes_are_never_exposed` scans the whole
serialized body for them.

---

## 3. Adding a market

The abstraction exists so this is the *only* work required. Nothing in
`analysis/` or `api/` changes.

### Step 1 — implement the provider

```python
# providers/oanda.py
from providers.base import MarketDataProvider, Quote, SymbolInfo, validate_candles


class OandaProvider(MarketDataProvider):
    name = 'oanda'
    market = 'forex'
    timeframes = ('5m', '15m', '1h', '4h', '1d')

    def initialize(self) -> None:
        ...                                  # open the session, cache instruments

    def list_symbols(self) -> list[str]:
        return ['EUR_USD', 'GBP_USD', ...]   # platform form

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        ...

    def fetch_candles(self, symbol, timeframe, limit=300):
        self.ensure_timeframe(timeframe)
        df = ...                             # venue call, mapped to CANDLE_COLUMNS
        return validate_candles(df, symbol)

    def fetch_quote(self, symbol: str) -> Quote:
        ...

    def reference_symbol(self):
        return None                          # FX has no BTC-style benchmark
```

`fetch_funding_rate()` and `reference_symbol()` are optional — the base class
returns `None`, so markets where they have no meaning need no special-casing
anywhere upstream.

### Step 2 — register it

```python
# providers/registry.py
def _install_builtin_providers(_register=register):
    from providers.binance import BinanceProvider
    from providers.oanda import OandaProvider

    _register(BinanceProvider)
    _register(OandaProvider)
```

### Step 3 — that's it

```bash
curl -X POST localhost:8000/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"market":"forex","symbol":"EUR_USD","timeframe":"1h"}'
```

`tests/test_providers.py::test_a_new_market_needs_no_analysis_changes` and
`tests/test_pipeline.py::test_pipeline_is_provider_agnostic` both register a
throwaway provider for a fictional market and run the real pipeline through it —
so the abstraction is enforced by the suite, not just by convention.

Planned providers: Binance ✅, Bybit, OANDA, MT5 bridge, TradingView, Polygon,
Twelve Data.

---

## 4. The trading guarantee

Three properties hold structurally, not by configuration:

1. **No credentials exist.** `ExchangeClient` takes no API key. There is no
   encryption key and no credential storage anywhere in the project.
2. **No background execution exists.** There is no loop and no scheduler. The
   service acts only when a user asks it to.
3. **No order path exists.** Every function that could construct, size, place,
   or close an order was deleted in Phase 0 — not disabled behind a flag.

`tests/test_api.py` asserts (3) directly: it probes for trade/order/position/
balance routes and scans the whole route table for forbidden path segments.
