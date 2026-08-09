"""The signal pipeline — the one place the stages are composed.

    Market Data Provider
      → Multi-Timeframe Engine   (trend / structure / entry rungs)
      → Analysis Engine          (indicators, regime, structure, S/R, fib, waves)
      → Confluence Engine        (eight module votes, aggregated)
      → Signal Generator         (direction, entry, stop, TP1/TP2/TP3)
      → Quality + Confidence     (deterministic, explainable)
      → Explanation Engine       (plain-English reasons)

`SignalPipeline.run()` is the single entry point the API calls. Every stage is
injected, so a stage can be swapped without touching the API or provider layers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from analysis.cache import RequestScope
from analysis.confluence import ConfluenceEngine, ConfluenceResult
from analysis.engine import AnalysisEngine
from analysis.explanation import Explanation, ExplanationEngine
from analysis.generator import SignalGenerator, TradingSignal
from analysis.intelligence import TradeIntelligence, build_intelligence
from analysis.modules import ModuleVote
from analysis.scoring import ConfidenceScorer, QualityScorer, Score
from analysis.timeframes import MultiTimeframeEngine, MultiTimeframePicture
from config.settings import Settings
from providers.base import MarketDataProvider
from v4.params import V4Params

logger = logging.getLogger(__name__)

# Fallback price precision when a venue cannot describe the instrument.
DEFAULT_PRICE_PRECISION = 8


@dataclass
class AnalysisResult:
    """Everything one /api/analyze call produced."""

    signal: TradingSignal
    mtf: MultiTimeframePicture
    confluence: ConfluenceResult
    quality: Score
    confidence: Score
    explanation: Explanation
    intelligence: TradeIntelligence
    elapsed_ms: int = 0
    candle_fetches: int = 0

    @property
    def votes(self) -> List[ModuleVote]:
        return self.confluence.votes


class SignalPipeline:
    """Composes every stage into one call."""

    def __init__(
        self,
        settings: Settings,
        params: Optional[V4Params] = None,
        analysis_engine: Optional[AnalysisEngine] = None,
        mtf_engine: Optional[MultiTimeframeEngine] = None,
        confluence_engine: Optional[ConfluenceEngine] = None,
        signal_generator: Optional[SignalGenerator] = None,
        quality_scorer: Optional[QualityScorer] = None,
        confidence_scorer: Optional[ConfidenceScorer] = None,
        explanation_engine: Optional[ExplanationEngine] = None,
    ) -> None:
        self.settings = settings
        self.params = params or V4Params()
        self.analysis_engine = analysis_engine or AnalysisEngine(settings, self.params)
        self.mtf_engine = mtf_engine or MultiTimeframeEngine(
            settings, self.analysis_engine
        )
        self.confluence_engine = confluence_engine or ConfluenceEngine(self.params)
        self.signal_generator = signal_generator or SignalGenerator(self.params)
        self.quality_scorer = quality_scorer or QualityScorer()
        self.confidence_scorer = confidence_scorer or ConfidenceScorer()
        self.explanation_engine = explanation_engine or ExplanationEngine()

    def run(
        self,
        provider: MarketDataProvider,
        symbol: str,
        timeframe: str,
        candle_limit: Optional[int] = None,
    ) -> AnalysisResult:
        """Analyse one symbol on one timeframe and return the full result.

        Raises:
            providers.base.ProviderError: On any market-data failure.
            ValueError: If there is not enough history on the selected timeframe.
        """
        started = time.perf_counter()
        scope = RequestScope()

        mtf = self.mtf_engine.build(provider, symbol, timeframe, scope=scope)
        confluence = self.confluence_engine.evaluate(mtf)

        precision = self._price_precision(provider, symbol, scope)
        quality = self.quality_scorer.score(mtf, confluence)
        signal = self.signal_generator.generate(mtf, confluence, quality, precision)
        confidence = self.confidence_scorer.score(mtf, confluence)
        explanation = self.explanation_engine.build(
            signal, mtf, confluence, quality, confidence
        )

        result = AnalysisResult(
            signal=signal,
            mtf=mtf,
            confluence=confluence,
            quality=quality,
            confidence=confidence,
            explanation=explanation,
            intelligence=None,          # filled below — needs the assembled result
            candle_fetches=scope.fetches,
        )
        # Phase 3 — the trade-intelligence layer reviews and explains the signal.
        # It is a pure read over `result`: it recomputes no market data and never
        # alters the direction, levels, or scores the engine produced.
        result.intelligence = build_intelligence(result)

        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            'ANALYZE | %s %s %s -> %s | quality=%d confidence=%d validation=%d '
            'health=%d★ | ladder=%s fetches=%d in %dms',
            provider.name, symbol, mtf.selected_timeframe, signal.direction,
            quality.value, confidence.value,
            result.intelligence.validation.score, result.intelligence.health.stars,
            '/'.join(mtf.ladder.timeframes), scope.fetches, result.elapsed_ms,
        )
        return result

    def _price_precision(
        self, provider: MarketDataProvider, symbol: str, scope: RequestScope
    ) -> int:
        """The instrument's tick precision, for rendering levels honestly."""
        def lookup() -> int:
            try:
                return provider.get_symbol_info(symbol).price_precision
            except Exception as exc:
                logger.debug('Symbol precision unavailable for %s: %s', symbol, exc)
                return DEFAULT_PRICE_PRECISION

        return int(scope.memoized(f'precision:{provider.name}:{symbol}', lookup))
