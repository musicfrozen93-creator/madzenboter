"""Analysis pipeline — the platform's single source of truth for signals.

    Multi-Timeframe Engine  ladder      → trend / structure / entry pictures
    Analysis Engine         market data → indicators · regime · structure ·
                                          S/R · Fibonacci · Elliott waves
    Confluence Engine       modules     → one direction, with conflicts named
    Signal Generator        direction   → entry · stop · TP1/TP2/TP3
    Quality / Confidence    everything  → two deterministic 0–100 scores
    Explanation Engine      everything  → plain-English reasons

Every stage consumes a `providers.MarketDataProvider`, never an exchange client,
so the pipeline works unchanged against any market. All technical calculations
are delegated to `signals/` and `v4/` — nothing here reimplements an indicator.
"""

from analysis.confluence import ConfluenceEngine, ConfluenceResult
from analysis.elliott import ElliottState, WaveCount, analyze_elliott
from analysis.engine import AnalysisEngine, IndicatorSnapshot, TechnicalPicture
from analysis.explanation import Explanation, ExplanationEngine
from analysis.fibonacci import FibonacciState, analyze_fibonacci
from analysis.generator import BUY, SELL, WAIT, SignalGenerator, TradingSignal
from analysis.levels import SupportResistance, Zone, detect_levels
from analysis.modules import MODULE_ORDER, MODULE_WEIGHTS, ModuleVote, evaluate_modules
from analysis.pipeline import AnalysisResult, SignalPipeline
from analysis.scoring import (
    MIN_TRADEABLE_QUALITY,
    ConfidenceScorer,
    QualityScorer,
    Score,
    ScoreComponent,
)
from analysis.structure import StructureState, Swing, analyze_structure, find_swings
from analysis.timeframes import (
    TIMEFRAME_LADDER,
    MultiTimeframeEngine,
    MultiTimeframePicture,
    TimeframeLadder,
    build_ladder,
)

__all__ = [
    'BUY',
    'MIN_TRADEABLE_QUALITY',
    'MODULE_ORDER',
    'MODULE_WEIGHTS',
    'SELL',
    'TIMEFRAME_LADDER',
    'WAIT',
    'AnalysisEngine',
    'AnalysisResult',
    'ConfidenceScorer',
    'ConfluenceEngine',
    'ConfluenceResult',
    'ElliottState',
    'Explanation',
    'ExplanationEngine',
    'FibonacciState',
    'IndicatorSnapshot',
    'ModuleVote',
    'MultiTimeframeEngine',
    'MultiTimeframePicture',
    'QualityScorer',
    'Score',
    'ScoreComponent',
    'SignalGenerator',
    'SignalPipeline',
    'StructureState',
    'SupportResistance',
    'Swing',
    'TechnicalPicture',
    'TimeframeLadder',
    'TradingSignal',
    'WaveCount',
    'Zone',
    'analyze_elliott',
    'analyze_fibonacci',
    'analyze_structure',
    'build_ladder',
    'detect_levels',
    'evaluate_modules',
    'find_swings',
]
