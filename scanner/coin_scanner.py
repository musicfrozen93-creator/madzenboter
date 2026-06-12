"""
Zentry Futures Core — Coin Scanner.

Scans all Binance USDT-M Futures pairs every 10 minutes, scores them
on volume, ATR, spread, and funding rate, and maintains a dynamic
watchlist of the top 20 highest-quality pairs.
"""

import logging
import time
from typing import List

import pandas as pd

from config.settings import Settings
from core.database import Database
from core.dto import CoinScore
from exchange.client import ExchangeClient
from signals.indicators import compute_atr

logger = logging.getLogger(__name__)

# Symbols containing these substrings are excluded (meme / delisted risk)
EXCLUDED_SUBSTRINGS = [
    '1000LUNC', 'LUNA2', 'LUNC/', 'UST/', 'DEFI/',
    'BTCDOM', 'FOOTBALL',
]


class CoinScanner:
    """Scans and scores USDT-M futures pairs for the dynamic watchlist.

    Scoring weights:
      Volume  35%  — higher 24h volume is better
      ATR     25%  — moderate ATR preferred (bell-curve)
      Spread  20%  — tighter spread is better
      Funding 20%  — funding rate closer to 0 is better
    """

    def __init__(
        self,
        exchange_client: ExchangeClient,
        settings: Settings,
        database: Database,
    ) -> None:
        self.exchange = exchange_client
        self.settings = settings
        self.database = database

    def scan(self) -> List[CoinScore]:
        """Run a full scan and return the ranked watchlist.

        Returns:
            List of CoinScore entries sorted by composite score descending,
            limited to max_watchlist_size (default 20).
        """
        logger.info('Starting coin scan...')
        start = time.time()

        try:
            all_symbols = self.exchange.get_all_futures_symbols()
            tickers = self.exchange.fetch_all_tickers()
        except Exception as e:
            logger.error('Failed to fetch market data for scan: %s', e)
            return self.database.get_watchlist()

        candidates: List[dict] = []

        for symbol in all_symbols:
            try:
                # ── Pre-filter ──
                if any(ex in symbol for ex in EXCLUDED_SUBSTRINGS):
                    continue

                ticker = tickers.get(symbol)
                if not ticker:
                    continue

                volume_24h = float(ticker.get('quoteVolume', 0) or 0)
                if volume_24h < self.settings.min_volume_24h:
                    continue

                last_price = float(ticker.get('last', 0) or 0)
                if last_price <= 0:
                    continue

                spread = float(ticker.get('spread', 0) or 0)

                # Funding rate
                funding_rate = self.exchange.fetch_funding_rate(symbol)
                if abs(funding_rate) > self.settings.max_funding_rate:
                    continue

                # ATR from 1h candles
                df = self.exchange.fetch_ohlcv(symbol, '1h', limit=50)
                if len(df) < 20:
                    continue

                atr_series = compute_atr(df['high'], df['low'], df['close'], period=14)
                atr_clean = atr_series.dropna()
                if atr_clean.empty:
                    continue

                current_atr = float(atr_clean.iloc[-1])

                candidates.append({
                    'symbol': symbol,
                    'volume_24h': volume_24h,
                    'atr': current_atr,
                    'spread': spread,
                    'last_price': last_price,
                    'funding_rate': funding_rate,
                })

                # Small delay to respect rate limits
                time.sleep(0.05)

            except Exception as e:
                logger.debug('Scan error for %s: %s', symbol, e)
                continue

        if not candidates:
            logger.warning('No candidates passed filters')
            return self.database.get_watchlist()

        # ── Score candidates ──
        scores = self._score_candidates(candidates)

        # Sort and trim
        scores.sort(key=lambda s: s.composite_score, reverse=True)
        watchlist = scores[: self.settings.max_watchlist_size]

        # ── V2: tier assignment by rank ──
        # core (full template rights) → secondary → rotation (capped below
        # the CORE template by the router). Expands breadth to ~50 symbols
        # while keeping full-size risk confined to the highest-quality tier.
        self._assign_tiers(watchlist)

        # Persist
        self.database.save_watchlist(watchlist)

        elapsed = time.time() - start
        symbols_str = ', '.join(s.symbol.split('/')[0] for s in watchlist[:5])
        logger.info(
            'Scan complete in %.1fs — %d candidates → top %d: [%s ...]',
            elapsed, len(candidates), len(watchlist), symbols_str,
        )

        return watchlist

    def _score_candidates(self, candidates: List[dict]) -> List[CoinScore]:
        """Compute normalised scores for each candidate.

        Args:
            candidates: List of raw candidate dicts.

        Returns:
            List of CoinScore instances with computed scores.
        """
        # Collect raw values for normalisation.
        # V2: ATR is normalised AS A PERCENTAGE OF PRICE — absolute ATR is
        # dominated by price level (BTC's ATR is in hundreds of dollars,
        # DOGE's in fractions of a cent), which made the V1 bell curve
        # price-level noise rather than a volatility preference.
        volumes = [c['volume_24h'] for c in candidates]
        atr_pcts = [
            (c['atr'] / c['last_price']) if c['last_price'] > 0 else 0.0
            for c in candidates
        ]

        vol_min, vol_max = min(volumes), max(volumes)
        atr_min, atr_max = min(atr_pcts), max(atr_pcts)

        scores: List[CoinScore] = []

        for c in candidates:
            # Volume score: linear min-max scaling (higher = better)
            if vol_max > vol_min:
                volume_score = (c['volume_24h'] - vol_min) / (vol_max - vol_min)
            else:
                volume_score = 0.5

            # ATR%-of-price score: bell curve — moderate volatility preferred
            atr_pct = (c['atr'] / c['last_price']) if c['last_price'] > 0 else 0.0
            if atr_max > atr_min:
                atr_normalized = (atr_pct - atr_min) / (atr_max - atr_min)
            else:
                atr_normalized = 0.5
            atr_score = 1.0 - abs(atr_normalized - 0.5) * 2.0
            atr_score = max(0.0, min(1.0, atr_score))

            # Spread score: tighter = better
            if c['last_price'] > 0:
                spread_ratio = c['spread'] / c['last_price']
                spread_score = max(0.0, 1.0 - spread_ratio * 10000)
            else:
                spread_score = 0.0

            # Funding score: closer to 0 = better
            if self.settings.max_funding_rate > 0:
                funding_score = 1.0 - (
                    abs(c['funding_rate']) / self.settings.max_funding_rate
                )
            else:
                funding_score = 0.5
            funding_score = max(0.0, min(1.0, funding_score))

            # Composite: weighted combination
            composite = (
                0.35 * volume_score
                + 0.25 * atr_score
                + 0.20 * spread_score
                + 0.20 * funding_score
            )

            # Prioritise small-account-friendly pairs: boost preferred symbols so
            # liquid, low-priced futures stay in the watchlist for $20–$100 accounts.
            boost = getattr(self.settings, 'preferred_symbol_score_boost', 0.0)
            if boost and self.settings.is_preferred_symbol(c['symbol']):
                composite = min(1.0, composite + boost)

            scores.append(
                CoinScore(
                    symbol=c['symbol'],
                    volume_24h=c['volume_24h'],
                    atr=c['atr'],
                    atr_score=round(atr_score, 4),
                    volume_score=round(volume_score, 4),
                    spread_score=round(spread_score, 4),
                    funding_rate=c['funding_rate'],
                    funding_score=round(funding_score, 4),
                    composite_score=round(composite, 4),
                )
            )

        return scores

    def _assign_tiers(self, watchlist: List[CoinScore]) -> None:
        """Assign V2 watchlist tiers by rank (list is already sorted desc).

        Ranks 1..core_size → 'core' (full template rights), the next
        secondary_size → 'secondary', the remainder → 'rotation' (the
        TemplateRouter caps rotation symbols below the CORE template, so
        the expanded breadth adds trade frequency without adding full-size
        risk on lower-ranked symbols).

        Args:
            watchlist: Ranked CoinScore list — tiers are set in place.
        """
        sizes = self.settings.watchlist_tier_sizes
        core_n = int(sizes.get('core', 20))
        secondary_n = int(sizes.get('secondary', 15))

        for rank, score in enumerate(watchlist):
            if rank < core_n:
                score.tier = 'core'
            elif rank < core_n + secondary_n:
                score.tier = 'secondary'
            else:
                score.tier = 'rotation'
