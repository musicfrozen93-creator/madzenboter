"""Request and response models for the Market Analysis API.

These Pydantic models ARE the contract between the Next.js frontend and this
service. The frontend displays what it receives and computes nothing.

Every value is calculated. There are no nullable score fields and no placeholder
stages: `quality` and `confidence` are always integers, and `entry`/`sl`/`tp` are
present whenever the signal is actionable.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from providers.base import TIMEFRAMES


# ─────────────────────────────────────────────
# Requests
# ─────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    """POST /api/analyze body."""

    market: str = Field(
        default='crypto',
        description="Market class, e.g. 'crypto'. Selects the default provider.",
        examples=['crypto'],
    )
    symbol: str = Field(
        description='Platform symbol, e.g. BTCUSDT.',
        examples=['BTCUSDT'],
        min_length=2,
        max_length=32,
    )
    timeframe: str = Field(
        description=(
            f'The timeframe to trade. One of: {", ".join(TIMEFRAMES)}. '
            'Higher timeframes are analysed internally for confirmation.'
        ),
        examples=['15m'],
    )
    provider: Optional[str] = Field(
        default=None,
        description='Optional explicit provider name. Omit to use the market default.',
        examples=['binance'],
    )
    enabled_indicators: Optional[List[str]] = Field(
        default=None,
        description=(
            'Optional list of analysis module keys to include (e.g. ["rsi","macd"]). '
            'Omit to use every module. Required core modules are always included; '
            'unknown names are ignored.'
        ),
        examples=[['rsi', 'macd', 'ema', 'market_structure']],
    )


# ─────────────────────────────────────────────
# Response components
# ─────────────────────────────────────────────

class IndicatorsModel(BaseModel):
    """Indicator readings on the last closed candle of the selected timeframe."""

    price: float
    rsi: float
    atr: float
    atr_pct: float
    atr_average_pct: float
    ema_fast: float
    ema_slow: float
    adx: float
    plus_di: float
    minus_di: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    bb_bandwidth: float
    volume: float
    volume_average: float
    volume_ratio: float


class ModuleBreakdownModel(BaseModel):
    """One analysis module's result and the points it contributed."""

    module: str = Field(description="e.g. 'trend', 'structure', 'elliott'")
    label: str = Field(description="Short state, e.g. 'Bullish BOS'")
    direction: str = Field(description="'bullish' | 'bearish' | 'neutral'")
    strength: float = Field(description='How strongly the module leans, 0–1')
    score: float = Field(description='Points contributed to the Quality Score')
    max_score: float = Field(description='Maximum points this module can contribute')
    detail: str


class WaveCountModel(BaseModel):
    """One Elliott wave count."""

    label: str = Field(description="e.g. 'Wave 3'")
    degree: str = Field(description="'impulse' | 'corrective'")
    direction: str
    confidence: float = Field(description='Probability of this count, 0–100')
    completion_pct: float = Field(description='Progress through the wave, 0–100')
    projected_target: Optional[float] = None
    rules_passed: List[str] = Field(default_factory=list)
    rules_violated: List[str] = Field(default_factory=list)
    guidelines_met: List[str] = Field(default_factory=list)


class ElliottModel(BaseModel):
    """The wave read, always with an alternative count."""

    current_wave: str
    direction: str
    confidence: float
    completion_pct: float
    primary: Optional[WaveCountModel] = None
    alternative: Optional[WaveCountModel] = None
    reason: str


class StructureModel(BaseModel):
    """Market structure on the selected timeframe."""

    trend: str
    event: Optional[str] = Field(default=None, description="'bos' | 'choch' | null")
    event_direction: Optional[str] = None
    last_swing_high: Optional[float] = None
    last_swing_low: Optional[float] = None
    swing_count: int = 0
    reason: str


class ZoneModel(BaseModel):
    """A support or resistance zone."""

    center: float
    lower: float
    upper: float
    touches: int
    strength: float


class LevelsModel(BaseModel):
    """Support and resistance around the current price."""

    nearest_support: Optional[ZoneModel] = None
    nearest_resistance: Optional[ZoneModel] = None
    supports: List[ZoneModel] = Field(default_factory=list)
    resistances: List[ZoneModel] = Field(default_factory=list)
    position: str = Field(description="e.g. 'just above support'")


class FibonacciModel(BaseModel):
    """Fibonacci geometry of the dominant leg."""

    valid: bool
    direction: Optional[str] = None
    leg_start: float = 0.0
    leg_end: float = 0.0
    current_ratio: float = 0.0
    in_golden_pocket: bool = False
    zone: str = ''
    retracements: Dict[str, float] = Field(default_factory=dict)
    extensions: Dict[str, float] = Field(default_factory=dict)


class RegimeModel(BaseModel):
    """Market regime classification."""

    direction: str
    volatility: str
    adx: float
    tradeable: bool
    reason: str


# ── Smart Money models (Phase 2, additive) ──

class OrderBlockModel(BaseModel):
    """The nearest active order block."""

    direction: str = 'neutral'
    state: Optional[str] = None          # 'fresh' | 'mitigated' | 'invalidated'
    top: Optional[float] = None
    bottom: Optional[float] = None
    distance_atr: float = 0.0
    strength: float = 0.0
    score: float = 0.0
    fresh_count: int = 0
    mitigated_count: int = 0
    reason: str = ''


class FairValueGapModel(BaseModel):
    """The nearest unfilled fair value gap."""

    direction: str = 'neutral'
    top: Optional[float] = None
    bottom: Optional[float] = None
    size_atr: float = 0.0
    distance_atr: float = 0.0
    score: float = 0.0
    unfilled_count: int = 0
    reason: str = ''


class LiquidityModel(BaseModel):
    """Liquidity pools and the most recent sweep."""

    direction: str = 'neutral'
    equal_highs: int = 0
    equal_lows: int = 0
    last_sweep_side: Optional[str] = None      # 'buy_side' | 'sell_side'
    grabbed: bool = False
    score: float = 0.0
    reason: str = ''


class VWAPModel(BaseModel):
    """Anchored VWAP and price's relationship to it."""

    vwap: float = 0.0
    distance_atr: float = 0.0
    above: bool = False
    below: bool = False
    trend: str = 'flat'
    direction: str = 'neutral'
    score: float = 0.0
    reason: str = ''


class MACDModel(BaseModel):
    """MACD line, signal, histogram, and crosses."""

    macd: float = 0.0
    signal: float = 0.0
    histogram: float = 0.0
    bullish_cross: bool = False
    bearish_cross: bool = False
    momentum: str = 'neutral'
    direction: str = 'neutral'
    score: float = 0.0
    reason: str = ''


class ADXTrendModel(BaseModel):
    """ADX trend strength and DI direction."""

    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    trend_strength: str = 'no_trend'     # 'strong' | 'weak' | 'no_trend'
    strong_trend: bool = False
    weak_trend: bool = False
    no_trend: bool = True
    direction: str = 'neutral'
    score: float = 0.0
    reason: str = ''


class PatternModel(BaseModel):
    """The best-matching chart pattern."""

    name: str = 'None'
    direction: str = 'neutral'
    strength: float = 0.0
    score: float = 0.0
    reason: str = ''


# ── ICT + MSNR contextual models (Phase 2A, additive) ──

class MSNRModel(BaseModel):
    """MSNR location context."""

    location: str = 'UNAVAILABLE'     # SUPPORT_ZONE | RESISTANCE_ZONE | MID_RANGE | NO_CLEAR_LEVEL | UNAVAILABLE
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    distance_to_support: Optional[float] = None
    distance_to_resistance: Optional[float] = None
    strength: float = 0.0
    score: float = 0.0
    explanation: str = ''


class DisplacementModel(BaseModel):
    """Displacement detection."""

    direction: str = 'UNAVAILABLE'    # BULLISH | BEARISH | NEUTRAL | UNAVAILABLE
    strength: float = 0.0
    has_structure_break: bool = False
    explanation: str = ''


class PremiumDiscountModel(BaseModel):
    """Premium / Discount zone."""

    zone: str = 'UNAVAILABLE'         # PREMIUM | EQUILIBRIUM | DISCOUNT | UNAVAILABLE
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    midpoint: Optional[float] = None
    price_position: float = 0.5
    explanation: str = ''


class ICTStructureModel(BaseModel):
    """ICT interpretation of market structure."""

    has_bos: bool = False
    bos_direction: Optional[str] = None
    has_mss: bool = False
    mss_direction: Optional[str] = None
    higher_highs: bool = False
    higher_lows: bool = False
    lower_highs: bool = False
    lower_lows: bool = False
    trend: str = 'range'
    strength: float = 0.0
    explanation: str = ''


class IctMsnrConfluenceModel(BaseModel):
    """ICT-MSNR relationship-based confluence."""

    direction: str = 'range'
    strength: float = 0.0
    score: float = 0.0
    bullish_elements: List[str] = Field(default_factory=list)
    bearish_elements: List[str] = Field(default_factory=list)
    neutral_elements: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    primary_reason: str = ''
    explanation: str = ''
    primary_blocker: Optional[str] = None
    secondary_blockers: List[str] = Field(default_factory=list)
    evidence_count: int = 0
    deduplicated_count: int = 0


class MarketQualityModel(BaseModel):
    """One market-quality check."""

    name: str
    passed: bool
    detail: str


class MarketContextModel(BaseModel):
    """Broad-market context and live quote."""

    benchmark_symbol: Optional[str] = None
    benchmark_trend: str = 'unknown'
    last_price: float = 0.0
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_pct: Optional[float] = None
    quote_volume_24h: Optional[float] = None


class ScoreComponentModel(BaseModel):
    """One line of a score breakdown."""

    name: str
    points: float
    max_points: float
    label: str
    detail: str


class ScoreDetailModel(BaseModel):
    """A score with its grade and full component breakdown."""

    value: int
    grade: str
    components: List[ScoreComponentModel] = Field(default_factory=list)


class ConfluenceModel(BaseModel):
    """How the module votes aggregated."""

    direction: str
    agreement: float = Field(description='Winner share of cast module weight, 0–1')
    bullish_weight: float
    bearish_weight: float
    timeframe_agreement: float = Field(description='Internal ladder agreement, 0–1')
    conflicts: List[str] = Field(default_factory=list)
    hard_conflicts: List[str] = Field(default_factory=list)
    reason: str
    # Phase 2A: ICT-MSNR contextual confluence summary.
    ict_msnr_direction: str = 'range'
    ict_msnr_strength: float = 0.0
    ict_msnr_score: float = 0.0


class AnalysisDetailModel(BaseModel):
    """The full technical picture behind the signal.

    Everything here describes the SELECTED timeframe. The internal higher
    timeframes are deliberately not exposed — only `timeframes_analyzed` reveals
    how many were consulted, with no detail the user did not ask for.
    """

    breakdown: List[ModuleBreakdownModel] = Field(
        default_factory=list,
        description='Per-module result and score contribution.',
    )
    trend: str
    structure: StructureModel
    elliott: ElliottModel
    fibonacci: FibonacciModel
    levels: LevelsModel
    indicators: IndicatorsModel
    regime: RegimeModel
    confluence: ConfluenceModel
    # Smart Money confirmation layers (Phase 2, additive).
    order_blocks: OrderBlockModel = Field(default_factory=OrderBlockModel)
    fair_value_gaps: FairValueGapModel = Field(default_factory=FairValueGapModel)
    liquidity: LiquidityModel = Field(default_factory=LiquidityModel)
    vwap: VWAPModel = Field(default_factory=VWAPModel)
    macd: MACDModel = Field(default_factory=MACDModel)
    adx: ADXTrendModel = Field(default_factory=ADXTrendModel)
    patterns: PatternModel = Field(default_factory=PatternModel)
    # ICT + MSNR contextual layers (Phase 2A, additive).
    msnr: MSNRModel = Field(default_factory=MSNRModel)
    displacement: DisplacementModel = Field(default_factory=DisplacementModel)
    premium_discount: PremiumDiscountModel = Field(default_factory=PremiumDiscountModel)
    ict_structure: ICTStructureModel = Field(default_factory=ICTStructureModel)
    ict_msnr_confluence: IctMsnrConfluenceModel = Field(default_factory=IctMsnrConfluenceModel)
    market_quality: List[MarketQualityModel] = Field(default_factory=list)
    market_conditions_clean: bool = True
    context: MarketContextModel
    candles_analyzed: int = 0
    timeframes_analyzed: int = Field(
        default=1,
        description='How many timeframes were analysed internally.',
    )


# ── Trade Intelligence models (Phase 3, additive) ──

class ValidationCheckModel(BaseModel):
    """One module's line in the validation checklist."""

    module: str
    label: str
    status: str            # 'confirmed' | 'neutral' | 'against'
    detail: str


class ValidationModel(BaseModel):
    """Signal validation review."""

    score: int
    status: str            # 'Excellent Validation' … 'Weak Validation'
    confirmed: int
    against: int
    neutral: int
    total: int
    checks: List[ValidationCheckModel] = Field(default_factory=list)


class HealthModel(BaseModel):
    """Overall signal health rating."""

    stars: int
    stars_display: str
    label: str             # 'Excellent' … 'Poor'
    composite: int
    summary: str


class GuideStepModel(BaseModel):
    """One step of the trade management guide."""

    number: int
    title: str
    detail: str


class TradeGuideModel(BaseModel):
    """The trade management guide."""

    tradeable: bool
    steps: List[GuideStepModel] = Field(default_factory=list)
    note: str = ''


class LifecycleModel(BaseModel):
    """Signal lifecycle snapshot."""

    status: str            # 'waiting' | 'triggered' | 'no_setup'
    status_label: str
    expiration: str
    recommend_fresh_analysis: bool
    stages: List[str] = Field(default_factory=list)
    current_stage: Optional[str] = None
    detail: str = ''


class RiskFactorModel(BaseModel):
    """One risk factor."""

    factor: str
    raises_risk: bool
    detail: str


class RiskModel(BaseModel):
    """Risk advisory."""

    level: str             # 'Low' | 'Medium' | 'High'
    summary: str
    factors: List[RiskFactorModel] = Field(default_factory=list)


class InvalidationConditionModel(BaseModel):
    """One invalidation condition."""

    source: str
    condition: str


class InvalidationModel(BaseModel):
    """Invalidation conditions for the signal."""

    summary: str
    conditions: List[InvalidationConditionModel] = Field(default_factory=list)


class NarrativeModel(BaseModel):
    """Beginner and professional explanations."""

    summary: str
    professional: List[str] = Field(default_factory=list)
    beginner: List[str] = Field(default_factory=list)


class IntelligenceModel(BaseModel):
    """The complete trade-intelligence layer (Phase 3).

    Reviews and explains the signal — it never changes the direction, levels, or
    scores the Analysis Engine produced.
    """

    validation: ValidationModel
    health: HealthModel
    trade_guide: TradeGuideModel
    lifecycle: LifecycleModel
    risk: RiskModel
    invalidation: InvalidationModel
    explanation: NarrativeModel


class AnalyzeResponse(BaseModel):
    """POST /api/analyze response."""

    signal: str = Field(description="'BUY', 'SELL', or 'WAIT'.")
    market: str
    provider: str
    symbol: str
    timeframe: str = Field(description='The timeframe the user selected.')

    quality: int = Field(description='Setup quality, 0–100.')
    quality_grade: str = Field(description="e.g. 'High Quality'")
    confidence: int = Field(description='Engine certainty, 0–100.')
    confidence_grade: str = Field(description="e.g. 'High'")
    enabled_indicators: List[str] = Field(
        default_factory=list,
        description='Analysis module keys that participated in this result.',
    )

    entry: Optional[float] = Field(default=None, description='Null when WAIT.')
    sl: Optional[float] = Field(default=None, description='Null when WAIT.')
    tp: List[float] = Field(
        default_factory=list,
        description='TP1, TP2, TP3 — empty when the signal is WAIT.',
    )
    risk_reward: Optional[float] = None
    risk_pct: Optional[float] = None
    rr_per_tp: List[float] = Field(
        default_factory=list,
        description='Per-target R:R (TP1, TP2, TP3) — empty when WAIT.',
    )

    headline: str
    reasons: List[str] = Field(
        default_factory=list,
        description='Every reason, prefixed ✓ (supports), ✕ (against), • (context).',
    )
    supporting_reasons: List[str] = Field(default_factory=list)
    opposing_reasons: List[str] = Field(default_factory=list)
    context_notes: List[str] = Field(default_factory=list)
    wait_reason: Optional[str] = Field(
        default=None,
        description='Why a signal was withheld. Present only when signal=WAIT.',
    )

    entry_basis: Optional[str] = None
    stop_basis: Optional[str] = None
    target_sources: List[str] = Field(default_factory=list)

    analysis: AnalysisDetailModel
    quality_detail: ScoreDetailModel
    confidence_detail: ScoreDetailModel
    # Phase 3 — the trade-intelligence review (additive; never alters the signal).
    intelligence: IntelligenceModel

    diagnostic: Optional[Dict] = Field(
        default=None,
        description='Full diagnostic breakdown: modules, gates, reasoning.',
    )

    generated_at: str = Field(description='UTC ISO-8601 timestamp.')
    elapsed_ms: int = 0


# ─────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────

class ProviderModel(BaseModel):
    """One registered market data provider."""

    provider: str
    market: str
    timeframes: List[str]
    is_default: bool = False


class MarketsResponse(BaseModel):
    """GET /api/markets — what this service can analyse."""

    markets: Dict[str, List[str]] = Field(
        description='Market class -> provider names serving it.'
    )
    providers: List[ProviderModel]


class SymbolsResponse(BaseModel):
    """GET /api/markets/{market}/symbols."""

    market: str
    provider: str
    timeframes: List[str]
    symbols: List[str]
    count: int


class IndicatorModel(BaseModel):
    """One analysis module, for the configuration UI."""

    key: str
    label: str
    category: str
    weight: int
    required: bool


class IndicatorsResponse(BaseModel):
    """GET /api/indicators — the analysis modules the engine supports."""

    indicators: List[IndicatorModel]


class HealthResponse(BaseModel):
    """GET /health."""

    status: str
    service: str
    version: str
    trading_enabled: bool = Field(
        default=False,
        description='Always false. This service can never place an order.',
    )
    providers_registered: List[str] = Field(default_factory=list)
    cache: Dict[str, float] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Uniform error body for every 4xx/5xx response."""

    error: str
    detail: Optional[str] = None
