"""Coin Scanner + Coin Ranking (spec §6).

Two stages: an eligibility FILTER (a symbol must be liquid, tight-spread, an
active linear USDT perp, with non-extreme funding and tradeable volatility), then
an opportunity RANKING that scores eligible candidates and prefers the best, most
uncorrelated setups. Both are pure over supplied market data (ticker/market/
funding dicts) — no network here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from v4.engines.base import V4Signal
from v4.params import V4Params


@dataclass(frozen=True)
class Candidate:
    """An eligible symbol carrying its regime signal and liquidity/volatility stats."""

    symbol: str
    signal: V4Signal
    quote_volume: float
    atr_pct: float


@dataclass(frozen=True)
class RankedCandidate:
    candidate: Candidate
    score: float
    breakdown: dict


class CoinScanner:
    """Eligibility filter — a symbol must pass ALL checks to be watchable."""

    def __init__(self, params: Optional[V4Params] = None) -> None:
        self.params = params or V4Params()

    def is_eligible(
        self,
        symbol: str,
        ticker: dict,
        market: dict,
        funding_rate: float = 0.0,
        atr_pct: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Return (eligible, reason). `atr_pct` is ATR/price on the trading TF."""
        p = self.params

        if not market.get('active', False):
            return False, 'market_inactive'
        if market.get('type') != 'swap' or not market.get('linear', False):
            return False, 'not_linear_swap'
        if market.get('settle') != 'USDT':
            return False, 'not_usdt_settled'

        last = float(ticker.get('last', 0) or 0)
        if last <= 0:
            return False, 'no_price'

        quote_volume = float(ticker.get('quoteVolume', 0) or 0)
        if quote_volume < p.min_quote_volume:
            return False, f'illiquid (quoteVol {quote_volume:.0f} < {p.min_quote_volume:.0f})'

        bid = float(ticker.get('bid', 0) or 0)
        ask = float(ticker.get('ask', 0) or 0)
        spread = float(ticker.get('spread', (ask - bid) if (ask and bid) else 0) or 0)
        if last > 0 and spread > 0:
            spread_pct = spread / last
            if spread_pct > p.scanner_max_spread_pct:
                return False, f'spread_too_wide ({spread_pct:.5f} > {p.scanner_max_spread_pct:.5f})'

        if abs(float(funding_rate or 0.0)) > p.funding_extreme_abs:
            return False, f'funding_extreme ({funding_rate:.5f})'

        if atr_pct is not None:
            if atr_pct < p.min_atr_pct:
                return False, f'atr_too_low ({atr_pct:.4f} < {p.min_atr_pct:.4f})'
            if atr_pct > p.max_atr_pct:
                return False, f'atr_too_high ({atr_pct:.4f} > {p.max_atr_pct:.4f})'

        return True, 'OK'


class CoinRanker:
    """Rank eligible candidates; prefer high-confluence, liquid, diversified setups."""

    def __init__(self, params: Optional[V4Params] = None) -> None:
        self.params = params or V4Params()

    def _atr_score(self, atr_pct: float) -> float:
        """Peaked score: best in the middle of the tradeable ATR band."""
        p = self.params
        lo, hi = p.min_atr_pct, p.max_atr_pct
        if atr_pct <= lo or atr_pct >= hi:
            return 0.0
        mid = (lo + hi) / 2.0
        # Triangular: 1.0 at mid, 0.0 at the edges.
        return max(0.0, 1.0 - abs(atr_pct - mid) / (mid - lo))

    def _liquidity_score(self, quote_volume: float) -> float:
        """Saturating liquidity score above the floor (diminishing returns)."""
        p = self.params
        if quote_volume <= p.min_quote_volume:
            return 0.0
        # 10× the floor saturates to ~1.0.
        return min(1.0, (quote_volume / p.min_quote_volume - 1.0) / 9.0)

    def rank(
        self,
        candidates: List[Candidate],
        open_long_symbols: Optional[set] = None,
        open_short_symbols: Optional[set] = None,
    ) -> List[RankedCandidate]:
        """Score and sort candidates (best first).

        Diversity penalty: a candidate whose side already has open exposure is
        penalized so the shortlist favors uncorrelated additions (spec §6).
        """
        open_long_symbols = open_long_symbols or set()
        open_short_symbols = open_short_symbols or set()
        heavy_side_count = {
            'long': len(open_long_symbols),
            'short': len(open_short_symbols),
        }

        ranked: List[RankedCandidate] = []
        for c in candidates:
            confluence_score = c.signal.confluence / 6.0
            atr_score = self._atr_score(c.atr_pct)
            liq_score = self._liquidity_score(c.quote_volume)
            diversity_penalty = 0.15 * heavy_side_count.get(c.signal.side, 0)
            score = (
                0.50 * confluence_score
                + 0.25 * atr_score
                + 0.25 * liq_score
                - diversity_penalty
            )
            ranked.append(RankedCandidate(
                candidate=c, score=score,
                breakdown={
                    'confluence': confluence_score, 'atr': atr_score,
                    'liquidity': liq_score, 'diversity_penalty': diversity_penalty,
                },
            ))
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked
