# ZenGrid Futures Core — Architecture Audit

Dark-Venus basket-recovery core for Binance USDT-M Futures. **10-symbol** correlated
watchlist (TRX, XRP, XLM, ADA, ALGO, HBAR, VET, LINK, DOT, ATOM), **15m** timeframe,
default leverage **8×**. Survival-first.

Audit covers the code under `madzenboter-main/madzenboter-main/`. 91 unit tests
pass. Multi-account platform infrastructure (DB models, encryption, subscriptions,
admin, logging, exchange execution) is unchanged.

---

## Requested-changes checklist (status)

| Item | Status | Notes |
|------|--------|-------|
| Exposure calculation | ✅ Investigated + fixed | Cap now uses INTENDED tier margins so a 2-layer basket (L1+L2=cap) is never false-blocked; L1 sized at fresh price; `EXPOSURE_DEBUG` log added. See **Exposure Logic**. |
| ROI exit system | ✅ Implemented | `TakeProfitManager.evaluate_exit` |
| Recovery ROI exit system | ✅ Implemented | Recovery baskets (≥2 layers); Tier 1 12%, Tier 2 10% |
| Layer 1 ROI exit | ✅ Implemented | L1-only baskets; Tier 1 12% ($0.24), Tier 2 10% ($0.40); `ROI_L1_EXIT` log |
| Hybrid recovery trigger | ✅ Implemented | ATR×2 OR Layer-1 floating loss ≥ $0.50, whichever first; `RECOVERY_TRIGGER` logs trigger type |
| Expanded watchlist | ✅ Implemented | 10 correlated symbols; risk unchanged (tier caps still bound margin/exposure/layers) |
| Tier-based position limits | ✅ Implemented | Tier 1 2 sym/4 pos, Tier 2 3 sym/6 pos |
| Correlation protection | ✅ Implemented | Strength score 0–4 + second-symbol rule |
| Protection lock system | ✅ Implemented | Equity floors $15/$30, permanent, admin reset |

---

## Entry Logic

Per-symbol, in `signals/signal_engine.py` (runs on shared public market data,
then fanned out to every eligible account):

1. Symbol must be one of TRX/XRP/XLM, else skip.
2. Fetch 15m OHLCV; warm up RSI(14), ATR(14), Bollinger(20, 2σ).
3. **Pre-trade risk filters** (skip with logged reason if ANY trips):
   spread too high (`> max_spread_pct`), ATR explosion (`ATR > 2.5× avg ATR`),
   news/oversized candle (`body > 2.5× ATR`), volume spike (`> 3× avg volume`).
4. **Mean-reversion conditions** (all required):
   - LONG: `RSI < 30` AND candle low touches the lower Bollinger band.
   - SHORT: `RSI > 70` AND candle high touches the upper Bollinger band.
5. **BTC 15m trend filter** (gates direction, runs before every basket):
   bullish (price > EMA200 AND EMA50 > EMA200) blocks SHORT; bearish blocks LONG;
   neutral/unknown allows both.
6. **Signal strength score (0–4)** computed for correlation protection.

Account-level entry gating happens in `grid/position_manager.open_position`, in
this order: bot-control → supported-symbol → tier resolution →
**[1] lock status → [2] daily profit → [3] daily loss** (via
`can_take_new_entry`, after refreshing locks from realised PnL and death
protection) → **[4] cooldown** → structural limits → correlation score →
exchange-safety sizing → execute.

## Exit Logic

Every management cycle (`manage_baskets`), priority order:

- **P0 — Account death protection:** equity < tier floor → PROTECTION_LOCK + close all.
- **P1 — Daily loss limit:** realised+unrealised ≤ −tier limit → close all + lock.
- **P2 — Basket exit:** `evaluate_exit` → fixed-USD TP or recovery ROI target (below).
- **P3 — Recovery layer:** Layer-1 drawdown ≥ ATR×2 → add Layer 2.

Daily profit target latches a new-entry lock (no closing).

## Recovery Logic

`grid/recovery.py` + `position_manager._add_recovery_layer`. Max **2 layers**
(never Layer 3+, never martingale). Layer 2 activates on a **HYBRID trigger** —
whichever occurs first:
- **ATR_TRIGGER:** price moves `ATR(14) × 2` against the Layer-1 entry, OR
- **LOSS_TRIGGER:** Layer-1 floating loss ≥ `recovery_loss_trigger_usd` ($0.50).

(`ATR_AND_LOSS` is logged if both hit in the same tick.) Layer-2 margin is the
basket's **locked tier** L2 (Tier 1 $4, Tier 2 $8). The exposure cap uses intended
tier margins (see Exposure Logic). The tier is locked at open (stored in
`basket.volatility`), so a later balance change (e.g. a deposit) never resizes
recovery, exposure, or TP.

## TP Logic

Two conditions; the basket closes on the **first** met. See the detailed section
"TP mechanics & examples" below.

## ROI Logic

`ROI = net basket PnL / total basket margin`, where net PnL = unrealised PnL −
estimated round-trip taker fees. Applied as a close condition for **recovery
baskets only** (≥2 layers): Tier 1 **12%**, Tier 2 **10%**. Because the ROI
dollar value ($0.72 / $1.20) is below the recovery USD target ($1.50 / $2.00),
ROI is the binding (earlier) exit for recovery baskets — freeing capital faster.

## Stop-Loss Logic

There is **no per-position or per-basket stop-loss** by design. Downside is
bounded by three layered account-level guards: the **daily loss limit**
(realised+unrealised), the permanent **death-protection** equity floor, and the
2-layer/exposure-capped basket structure. (Per the original Dark-Venus spec, the
$3/$4 daily loss limit *is* the stop.)

## Daily Profit Logic

Per-account, tier target (Tier 1 $3, Tier 2 $4). Measured as realised+unrealised
**trading** PnL (never wallet balance). On reach: latch `daily_profit_locked`
(blocks new entries), keep managing open baskets, auto-clear on UTC reset.

## Daily Loss Logic

Per-account, tier limit (Tier 1 $3, Tier 2 $4). Measured as realised+unrealised
trading PnL. On reach: close ALL baskets + recovery layers immediately (does not
wait for realisation), latch `daily_loss_locked`, auto-clear on UTC reset.

## Protection-Lock Logic

`RiskManager.check_account_death_protection`. Equity (wallet + floating PnL) <
tier floor ($15 / $30) → set `protection_locked` (DB-persisted, account-scoped),
close all baskets, block all entries **permanently**. NOT cleared by the UTC
reset. Admin reset only: `python main.py --clear-protection <ACCOUNT_ID>`.

## Exposure Logic (investigation + fix)

**Investigation.** `Basket.total_margin` = Σ active-layer margins, where each
layer's `margin = actual_filled_qty × fill_price / leverage`. The reported
symptom — *"Current Exposure = 5.00"* for an intended Layer-1 margin of 2.00 —
is reproducible only when the **Layer-1 quantity was sized at a stale/low price**
and then filled at a much higher price: `qty = round(2×8 / sizing_price)` and
`actual_margin = qty × fill_price / 8`. If `fill_price ≫ sizing_price` the
recorded margin inflates above the intended $2, which then made
`actual_current ($5) + intended_L2 ($4) = $9 > $6` and **wrongly blocked the
legitimate recovery**.

**Fix (two parts):**
1. **Size Layer 1 at a fresh execution-time price** (`fetch_ticker` at open,
   fallback to the signal price), so `qty` matches the real price and the recorded
   margin stays ≈ the intended tier margin.
2. **Cap the recovery on INTENDED tier margins**, not fill-inflated actuals:
   `intended_current (Σ tier layer margins of existing layers) + intended_L2 ≤
   tier cap`. By configuration `L1 + L2 = cap` exactly, so a valid 2-layer basket
   is **never** false-blocked; the cap only ever blocks a genuine misconfiguration
   (also caught by `settings.validate`). This does not weaken any protection —
   max layers (2) and the exchange's own margining still bound real risk.

**Mathematical proof (Tier 1, 8×).** Intended L1 margin = $2, L2 margin = $4,
cap = $6. Recovery decision: `intended_current = tier.layer1_margin = $2`,
`intended_L2 = $4`, `projected = $2 + $4 = $6 ≤ $6` → **allowed**, for every
Tier-1 basket regardless of recorded actuals. Tier 2: `$4 + $8 = $12 ≤ $12` →
allowed. ∎

New `EXPOSURE_DEBUG` log emits fill qty, fill price, notional, leverage, margin
used, current exposure, requested exposure, and the exposure limit on every
recovery, so the math is fully traceable in production.

## Position Limits

Per-tier: max active symbols (Tier 1 **2**, Tier 2 **3**), max positions (Tier 1
**4**, Tier 2 **6** = symbols × 2 layers), max 1 basket/symbol, max 2 layers/basket.

## Correlation Protection

TRX/XRP/XLM treated as correlated. Strength score (0–4): +1 extreme RSI (<20/>80),
+1 strong Bollinger penetration (close beyond band), +1 BTC strongly aligned, +1
good spread & liquidity. New basket requires score **≥2** with 0 open and **≥3**
with 1+ open; rejected once tier max symbols is reached.

## Account Isolation

Every account has its own `RiskManager` and an `AccountDatabaseWrapper` that
prefixes all state keys `account_<id>_…` (in `bot_state`) and forces
account-scoped trade/basket queries. Daily counters, locks, cooldowns, basket and
recovery state, and protection locks are fully independent. A per-account limit
never affects another account and never globally stops the bot. All locks persist
across restart/crash.

## Risk Management

Layered, survival-first: pre-trade filters → BTC gate → correlation score →
fixed tier sizing (no balance scaling / martingale) → exposure cap → daily
profit/loss locks → permanent death protection. Leverage fixed (8× default,
5–10× admin, 10× hard cap), never dynamic. Partial fills tracked as actual
qty/margin. Exchange-safety validation (min notional, min qty, step, precision)
before every order.

---

## Expectations (qualitative)

These are **structural expectations**, not backtested guarantees — actual results
depend on TRX/XRP/XLM 15m volatility and BTC regime.

- **Trade frequency:** Higher than the previous build (RSI 30/70 + BB touch on 3
  symbols, 15m). The correlation score gate (≥2/≥3) filters weak setups, so a
  meaningful fraction of touches are rejected. Rough order: a few setups per
  symbol per day in active markets; fewer in quiet/aligned-against regimes.
- **Basket duration:** Shorter than before for recovery baskets thanks to the ROI
  exit (close at $0.72/$1.20 instead of $1.50/$2.00). Layer-1-only baskets close
  at $0.50/$0.80. Many baskets resolve within a few 15m candles; recovery baskets
  can run longer if price keeps moving adversely.
- **Daily profit range:** Capped per account at the tier target ($3 / $4); the
  lock stops new entries once reached. Typical realised days land between $0 and
  the cap.
- **Daily drawdown range:** Bounded by the daily loss limit ($3 / $4, on
  realised+unrealised) and ultimately the death floor. Worst realistic daily
  drawdown ≈ the tier loss limit plus slippage on the forced close-all.

---

## TP mechanics & examples

### How Layer 1 TP works
A Layer-1-only basket closes on the **first** of: the Layer-1 USD target
(**Tier 1 $0.50**, **Tier 2 $0.80**) or the Layer-1 **ROI target**
(**Tier 1 12% → $0.24**, **Tier 2 10% → $0.40**). The ROI dollar value is lower,
so ROI is the binding exit and logs `ROI_L1_EXIT`.

### How Recovery Basket TP works
Once Layer 2 is added (≥2 layers), the basket closes on the **first** of:
- **USD target:** Tier 1 **$1.50**, Tier 2 **$2.00** (net), or
- **ROI target:** `net PnL / total margin ≥ tier ROI` — Tier 1 **12%**, Tier 2 **10%**.

Because the ROI dollar value is lower, ROI is evaluated first and is the one that
actually fires.

### How USD TP is calculated
`net_pnl = unrealised_pnl(price) − (total_qty × price × taker_fee × 2)`.
Close when `net_pnl ≥ tier_usd_target` (target chosen by layer count).

### How ROI TP is calculated
`roi = net_pnl / total_basket_margin` (total margin = Σ actual layer margins).
Close when `roi ≥ tier roi target`: Layer-1-only uses `layer1_roi_target`,
recovery uses `recovery_roi_target` (both 0.12 / 0.10). Equivalent dollar trigger
= `total_margin × roi_target`.

### Which condition closes first
For **every** basket the ROI dollar value is below the matching USD target, so as
profit rises it crosses ROI first → **ROI closes first** (`ROI_L1_EXIT` for
Layer-1-only, `ROI_RECOVERY_EXIT` for recovery). The USD target is effectively a
ceiling that only matters if you raise the ROI target above it.

### Real examples

**Tier 1 (account $20–$39.99), 8× leverage.**
- *Layer-1-only basket:* L1 margin $2. USD target $0.50, ROI 12% = **$0.24** →
  closes via **`roi_l1`** at ≈$0.24 net.
- *Recovery basket:* L1 $2 + L2 $4 = total margin **$6**. USD target $1.50,
  ROI 12% = **$0.72** → closes via **`roi_recovery`** at ≈$0.72 net.

**Tier 2 (account $40+), 8× leverage.**
- *Layer-1-only basket:* L1 margin $4. USD target $0.80, ROI 10% = **$0.40** →
  closes via **`roi_l1`** at ≈$0.40 net.
- *Recovery basket:* L1 $4 + L2 $8 = total margin **$12**. USD target $2.00,
  ROI 10% = **$1.20** → closes via **`roi_recovery`** at ≈$1.20 net.

---

## Full trade-closure audit

**What closes a BASKET** (in `manage_baskets`, priority order):

| Priority | Condition | Code | Reason logged | Scope |
|----------|-----------|------|---------------|-------|
| P0 | Equity (wallet + floating) < tier floor ($15/$30) | `check_account_death_protection` | `protection_lock` | ALL baskets, permanent lock |
| P1 | Realised+unrealised daily PnL ≤ −tier limit ($3/$4) | `check_loss_limit` | `daily_loss_limit` | ALL baskets, lock to UTC reset |
| P2a | L1-only ROI ≥ tier L1 ROI (12%/10%) | `evaluate_exit` | `roi_l1` | this basket |
| P2b | Recovery ROI ≥ tier ROI (12%/10%) | `evaluate_exit` | `roi_recovery` | this basket |
| P2c | Net PnL ≥ tier USD target | `evaluate_exit` | `basket_tp` | this basket |
| — | Admin force-close | `request_force_close_all` | `force_close_all` | ALL baskets |
| — | Exchange position vanished (manual/liquidation) | `reconcile_baskets` | finalised | this basket |

**What closes a TRADE:** a "trade" record is written exactly when a basket
closes (any reason above) — `close_basket` is the single closure path that
persists a `TradeModel`. There is no separate per-layer close except internal
recovery additions (which are opens, not closes).

**What LOCKS an account** (blocks new entries; per-account, DB-persisted):

| Lock | Trigger | Cleared by |
|------|---------|-----------|
| `daily_profit_locked` | realised+unrealised ≥ tier profit target | UTC-day reset |
| `daily_loss_locked` | realised+unrealised ≤ −tier loss limit | UTC-day reset |
| `protection_locked` | equity < tier floor | **admin only** (`--clear-protection`) |
| `emergency_shutdown` | manual/system | admin (`--clear-shutdown`) |

**Why "profitable trades remained open" (root cause + fix):** previously the only
exit for a Layer-1-only basket was the fixed USD target ($0.50/$0.80). A basket
that became profitable but stalled below that target (e.g. +$0.30) stayed open
indefinitely. **Fix:** the new **Layer-1 ROI exit** closes it at the tier ROI
($0.24/$0.40), which is reached first — so profitable baskets now realise quickly.
The recovery ROI exit (prior update) does the same for 2-layer baskets.

---

## Remaining weaknesses

1. **No per-position stop-loss.** A single basket can sit deeply underwater
   (Layer 2 only doubles down once) until the *account-level* daily loss /
   death-protection guard fires. This is by design (Dark-Venus), but it means a
   fast adverse move can take an account close to its daily loss limit in one
   basket before any per-basket cut.
2. **Death protection uses real equity (wallet + floating).** Deposits raise
   equity and can move an account away from its floor; this is intentional (real
   survival value) but differs from the deposit-immune daily PnL. Flagged for
   awareness — say the word to make it trade-derived instead.
3. **ROI exit makes the recovery USD target effectively dead** (ROI $ < USD $).
   That is the intended behaviour, but if you later raise ROI above the USD
   equivalent the USD target would take over — worth keeping in mind when tuning.
4. **Correlation score requires market data quality.** The "good spread &
   liquidity" point depends on ticker spread + volume; on testnet or thin feeds
   it may rarely score, biasing scores lower and reducing 2nd/3rd baskets.
5. **Strength score uses the forming 15m candle** (same as entry), so RSI/BB/score
   can flicker intra-candle. Acceptable for mean reversion but not bar-confirmed.
6. **Global, not per-symbol thresholds.** `max_spread_pct` (0.10%) and the
   volume/ATR filters are one-size-fits-all across the now-10 symbols, which span
   very different prices/tick sizes (VET ~$0.03 vs LINK ~$15). A single threshold
   is a compromise and may admit/reject unevenly across the watchlist.
7. **Flat `recovery_loss_trigger_usd` ($0.50) across tiers.** On Tier 2's larger
   $4 Layer-1 margin, $0.50 is a smaller % adverse move than on Tier 1's $2, so
   Layer 2 activates relatively sooner for Tier 2. Intentional per the spec, but
   worth noting; could be made tier-scaled.
8. **Lower per-trade profit from the L1 ROI exit.** Closing L1 baskets at $0.24 /
   $0.40 (vs $0.50 / $0.80) means more baskets are needed to reach the daily
   target — more turnover, more fee drag, more exchange round-trips.

## Recommended improvements

1. **Optional per-basket safety stop** (configurable, off by default) as a
   backstop independent of the daily limit — e.g. close a basket at `−X%` of its
   margin even before the account limit. (Addresses weakness #1.)
2. **Per-symbol filter thresholds** (spread/volume/ATR) for the 10-symbol
   watchlist, since tick sizes and typical spreads differ widely.
3. **Bar-confirmed entries/score** (use the last *closed* 15m candle) to remove
   intra-candle flicker, at the cost of slightly later entries.
4. **Tier-scaled `recovery_loss_trigger_usd`** (e.g. % of L1 margin) for
   consistent recovery behaviour across tiers.
5. **Admin REST endpoint** for protection-lock reset (today it's the CLI
   `--clear-protection`), if admins should clear it from the dashboard.
6. **Watchlist scoring/rotation** — with 10 symbols you could rank candidates by
   strength score and prefer the strongest setups when at the symbol cap.
6. **Admin REST endpoint** for protection-lock reset (today it's the CLI
   `--clear-protection`), if you want admins to clear it from the dashboard.
