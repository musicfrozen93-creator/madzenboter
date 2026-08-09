"""Market-quality filters.

Pure checks that decide whether market CONDITIONS are clean enough for a setup
to be trustworthy — independent of what the setup is. A tripped filter is a
reason to downgrade or withhold a signal, never a reason to trade.

Checks:
  • spread_too_high  — quoted spread / price above the ceiling
  • atr_explosion    — current ATR far above its recent average
  • news_candle      — last candle body far larger than ATR
  • volume_spike     — last volume far above its recent average

Network-free and stateless, so both the analysis pipeline and the mean-reversion
setup detector use the same implementation rather than each keeping its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

from config.settings import Settings
from signals.indicators import compute_atr


@dataclass(frozen=True)
class FilterResult:
    """Outcome of one market-quality check."""

    name: str
    passed: bool
    detail: str
    observed: Optional[float] = None
    threshold: Optional[float] = None


def evaluate_market_quality(
    df: pd.DataFrame,
    atr: float,
    spread: float,
    price: float,
    settings: Settings,
) -> List[FilterResult]:
    """Run every market-quality check and return one result per check.

    Args:
        df: OHLCV candles, oldest first.
        atr: Current ATR reading for the analysed timeframe.
        spread: Absolute quoted spread (ask − bid).
        price: Current price.
        settings: Thresholds.

    Returns:
        One FilterResult per check, in a stable order.
    """
    s = settings
    lookback = max(5, s.risk_filter_lookback)
    results: List[FilterResult] = []

    # ── Spread ──
    spread_pct = (spread / price) if price > 0 else 0.0
    results.append(FilterResult(
        name='spread',
        passed=spread_pct <= s.max_spread_pct,
        detail=(
            f'spread {spread_pct:.4%} vs ceiling {s.max_spread_pct:.4%}'
        ),
        observed=spread_pct,
        threshold=s.max_spread_pct,
    ))

    # ── ATR explosion (current ATR vs its recent average) ──
    atr_series = compute_atr(df['high'], df['low'], df['close'], period=s.atr_period).dropna()
    if len(atr_series) >= lookback:
        avg_atr = float(atr_series.iloc[-lookback:].mean())
        limit = s.atr_explosion_multiplier * avg_atr
        results.append(FilterResult(
            name='atr_explosion',
            passed=not (avg_atr > 0 and atr > limit),
            detail=(
                f'atr {atr:.8f} vs {s.atr_explosion_multiplier:.1f}x average '
                f'{avg_atr:.8f}'
            ),
            observed=atr,
            threshold=limit if avg_atr > 0 else None,
        ))
    else:
        results.append(FilterResult(
            name='atr_explosion',
            passed=True,
            detail='insufficient history to judge — not penalised',
        ))

    # ── News / oversized candle (last body vs ATR) ──
    last = df.iloc[-1]
    body = abs(float(last['close']) - float(last['open']))
    body_limit = s.news_candle_atr_multiplier * atr
    results.append(FilterResult(
        name='news_candle',
        passed=not (atr > 0 and body > body_limit),
        detail=(
            f'last body {body:.8f} vs {s.news_candle_atr_multiplier:.1f}x atr '
            f'{atr:.8f}'
        ),
        observed=body,
        threshold=body_limit if atr > 0 else None,
    ))

    # ── Volume spike (last volume vs recent average) ──
    if 'volume' in df.columns and len(df) >= lookback + 1:
        recent = df['volume'].iloc[-(lookback + 1):-1]
        avg_vol = float(recent.mean()) if not recent.empty else 0.0
        last_vol = float(last['volume'])
        vol_limit = s.volume_spike_multiplier * avg_vol
        results.append(FilterResult(
            name='volume_spike',
            passed=not (avg_vol > 0 and last_vol > vol_limit),
            detail=(
                f'last volume {last_vol:.0f} vs {s.volume_spike_multiplier:.1f}x '
                f'average {avg_vol:.0f}'
            ),
            observed=last_vol,
            threshold=vol_limit if avg_vol > 0 else None,
        ))
    else:
        results.append(FilterResult(
            name='volume_spike',
            passed=True,
            detail='insufficient history to judge — not penalised',
        ))

    return results


def first_failure(results: List[FilterResult]) -> Optional[FilterResult]:
    """The first tripped filter, or None when market conditions are clean."""
    return next((r for r in results if not r.passed), None)
