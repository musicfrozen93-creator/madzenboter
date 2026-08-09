"""Translate pipeline dataclasses into the API response models.

Keeping this separate means the pipeline never imports Pydantic and the API
never reaches into pipeline internals.

Only the SELECTED timeframe is serialized. The internal ladder rungs informed the
result but are not exposed — the user asked about one timeframe and gets one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from analysis.elliott import ElliottState, WaveCount
from analysis.levels import SupportResistance, Zone
from analysis.pipeline import AnalysisResult
from analysis.scoring import Score
from api.schemas import (
    ADXTrendModel,
    AnalysisDetailModel,
    AnalyzeResponse,
    ConfluenceModel,
    ElliottModel,
    FairValueGapModel,
    FibonacciModel,
    GuideStepModel,
    HealthModel,
    IndicatorsModel,
    IntelligenceModel,
    InvalidationConditionModel,
    InvalidationModel,
    LevelsModel,
    LifecycleModel,
    LiquidityModel,
    MACDModel,
    MarketContextModel,
    MarketQualityModel,
    ModuleBreakdownModel,
    NarrativeModel,
    OrderBlockModel,
    PatternModel,
    RegimeModel,
    RiskFactorModel,
    RiskModel,
    ScoreComponentModel,
    ScoreDetailModel,
    StructureModel,
    TradeGuideModel,
    ValidationCheckModel,
    ValidationModel,
    VWAPModel,
    WaveCountModel,
    ZoneModel,
)

# How many zones per side to expose. Beyond this they are noise for a UI.
MAX_ZONES = 4


def _score_detail(score: Score) -> ScoreDetailModel:
    return ScoreDetailModel(
        value=score.value,
        grade=score.grade,
        components=[
            ScoreComponentModel(
                name=c.name, points=round(c.points, 1), max_points=c.max_points,
                label=c.label, detail=c.detail,
            )
            for c in score.components
        ],
    )


def _zone(zone: Optional[Zone]) -> Optional[ZoneModel]:
    if zone is None:
        return None
    return ZoneModel(
        center=zone.center, lower=zone.lower, upper=zone.upper,
        touches=zone.touches, strength=zone.strength,
    )


def _levels(levels: SupportResistance, atr: float) -> LevelsModel:
    return LevelsModel(
        nearest_support=_zone(levels.nearest_support),
        nearest_resistance=_zone(levels.nearest_resistance),
        supports=[_zone(z) for z in levels.supports[:MAX_ZONES]],
        resistances=[_zone(z) for z in levels.resistances[:MAX_ZONES]],
        position=levels.position_label(atr),
    )


def _wave(count: Optional[WaveCount]) -> Optional[WaveCountModel]:
    if count is None:
        return None
    return WaveCountModel(
        label=count.label,
        degree=count.degree,
        direction=count.direction,
        confidence=count.confidence,
        completion_pct=count.completion_pct,
        projected_target=count.projected_target,
        rules_passed=count.rules_passed,
        rules_violated=count.rules_violated,
        guidelines_met=count.guidelines_met,
    )


def _order_blocks(ob) -> OrderBlockModel:
    nearest = ob.nearest
    return OrderBlockModel(
        direction=ob.direction,
        state=nearest.state if nearest else None,
        top=nearest.top if nearest else None,
        bottom=nearest.bottom if nearest else None,
        distance_atr=ob.distance_atr,
        strength=nearest.strength if nearest else 0.0,
        score=ob.score,
        fresh_count=len(ob.fresh),
        mitigated_count=len(ob.mitigated),
        reason=ob.reason,
    )


def _fair_value_gaps(fvg) -> FairValueGapModel:
    nearest = fvg.nearest
    return FairValueGapModel(
        direction=fvg.direction,
        top=nearest.top if nearest else None,
        bottom=nearest.bottom if nearest else None,
        size_atr=nearest.size_atr if nearest else 0.0,
        distance_atr=fvg.distance_atr,
        score=fvg.score,
        unfilled_count=len(fvg.unfilled),
        reason=fvg.reason,
    )


def _liquidity(liq) -> LiquidityModel:
    sweep = liq.last_sweep
    return LiquidityModel(
        direction=liq.direction,
        equal_highs=liq.equal_highs,
        equal_lows=liq.equal_lows,
        last_sweep_side=sweep.side if sweep else None,
        grabbed=sweep.grabbed if sweep else False,
        score=liq.score,
        reason=liq.reason,
    )


def _vwap(vwap) -> VWAPModel:
    return VWAPModel(
        vwap=vwap.vwap, distance_atr=vwap.distance_atr,
        above=vwap.above, below=vwap.below, trend=vwap.trend,
        direction=vwap.direction, score=vwap.score, reason=vwap.reason,
    )


def _macd(macd) -> MACDModel:
    return MACDModel(
        macd=macd.macd, signal=macd.signal, histogram=macd.histogram,
        bullish_cross=macd.bullish_cross, bearish_cross=macd.bearish_cross,
        momentum=macd.momentum, direction=macd.direction,
        score=macd.score, reason=macd.reason,
    )


def _adx(adx) -> ADXTrendModel:
    return ADXTrendModel(
        adx=adx.adx, plus_di=adx.plus_di, minus_di=adx.minus_di,
        trend_strength=adx.trend_strength, strong_trend=adx.strong_trend,
        weak_trend=adx.weak_trend, no_trend=adx.no_trend,
        direction=adx.direction, score=adx.score, reason=adx.reason,
    )


def _patterns(pat) -> PatternModel:
    return PatternModel(
        name=pat.name, direction=pat.direction,
        strength=pat.pattern.strength if pat.pattern else 0.0,
        score=pat.score, reason=pat.reason,
    )


def _elliott(state: ElliottState) -> ElliottModel:
    return ElliottModel(
        current_wave=state.label,
        direction=state.direction,
        confidence=state.confidence,
        completion_pct=state.primary.completion_pct if state.primary else 0.0,
        primary=_wave(state.primary),
        alternative=_wave(state.alternative),
        reason=state.reason,
    )


def _intelligence(it) -> IntelligenceModel:
    """Serialize the Phase 3 trade-intelligence layer."""
    return IntelligenceModel(
        validation=ValidationModel(
            score=it.validation.score,
            status=it.validation.status,
            confirmed=it.validation.confirmed,
            against=it.validation.against,
            neutral=it.validation.neutral,
            total=it.validation.total,
            checks=[
                ValidationCheckModel(
                    module=c.module, label=c.label, status=c.status, detail=c.detail,
                )
                for c in it.validation.checks
            ],
        ),
        health=HealthModel(
            stars=it.health.stars,
            stars_display=it.health.stars_display,
            label=it.health.label,
            composite=it.health.composite,
            summary=it.health.summary,
        ),
        trade_guide=TradeGuideModel(
            tradeable=it.trade_guide.tradeable,
            steps=[
                GuideStepModel(number=s.number, title=s.title, detail=s.detail)
                for s in it.trade_guide.steps
            ],
            note=it.trade_guide.note,
        ),
        lifecycle=LifecycleModel(
            status=it.lifecycle.status,
            status_label=it.lifecycle.status_label,
            expiration=it.lifecycle.expiration,
            recommend_fresh_analysis=it.lifecycle.recommend_fresh_analysis,
            stages=it.lifecycle.stages,
            current_stage=it.lifecycle.current_stage,
            detail=it.lifecycle.detail,
        ),
        risk=RiskModel(
            level=it.risk.level,
            summary=it.risk.summary,
            factors=[
                RiskFactorModel(factor=f.factor, raises_risk=f.raises_risk, detail=f.detail)
                for f in it.risk.factors
            ],
        ),
        invalidation=InvalidationModel(
            summary=it.invalidation.summary,
            conditions=[
                InvalidationConditionModel(source=c.source, condition=c.condition)
                for c in it.invalidation.conditions
            ],
        ),
        explanation=NarrativeModel(
            summary=it.narrative.summary,
            professional=it.narrative.professional,
            beginner=it.narrative.beginner,
        ),
    )


def to_analyze_response(result: AnalysisResult) -> AnalyzeResponse:
    """Serialize a completed pipeline run into the public response shape."""
    signal = result.signal
    mtf = result.mtf
    picture = mtf.entry
    ind = picture.indicators
    quote = picture.quote
    confluence = result.confluence

    # Pair each module vote with the points it contributed to the Quality Score.
    quality_points = {c.name: c for c in result.quality.components}
    breakdown = [
        ModuleBreakdownModel(
            module=vote.module,
            label=vote.label,
            direction=vote.direction,
            strength=round(vote.strength, 4),
            score=round(quality_points[vote.module].points, 1)
            if vote.module in quality_points else 0.0,
            max_score=quality_points[vote.module].max_points
            if vote.module in quality_points else 0.0,
            detail=vote.detail,
        )
        for vote in confluence.votes
    ]

    return AnalyzeResponse(
        signal=signal.direction,
        market=signal.market,
        provider=signal.provider,
        symbol=signal.symbol,
        timeframe=signal.timeframe,

        quality=result.quality.value,
        quality_grade=result.quality.grade,
        confidence=result.confidence.value,
        confidence_grade=result.confidence.grade,

        entry=signal.entry,
        sl=signal.stop_loss,
        tp=list(signal.take_profits),
        risk_reward=signal.risk_reward,
        risk_pct=signal.risk_pct,

        headline=result.explanation.headline,
        reasons=result.explanation.all_reasons,
        supporting_reasons=result.explanation.supporting,
        opposing_reasons=result.explanation.against,
        context_notes=result.explanation.notes,
        wait_reason=signal.wait_reason,

        entry_basis=signal.entry_basis,
        stop_basis=signal.stop_basis,
        target_sources=list(signal.target_sources),

        analysis=AnalysisDetailModel(
            breakdown=breakdown,
            trend=picture.trend_direction,
            structure=StructureModel(
                trend=picture.structure.trend,
                event=picture.structure.event,
                event_direction=picture.structure.event_direction,
                last_swing_high=picture.structure.last_swing_high,
                last_swing_low=picture.structure.last_swing_low,
                swing_count=len(picture.structure.swings),
                reason=picture.structure.reason,
            ),
            elliott=_elliott(picture.elliott),
            fibonacci=FibonacciModel(
                valid=picture.fibonacci.valid,
                direction=picture.fibonacci.direction,
                leg_start=picture.fibonacci.leg_start,
                leg_end=picture.fibonacci.leg_end,
                current_ratio=picture.fibonacci.current_ratio,
                in_golden_pocket=picture.fibonacci.in_golden_pocket,
                zone=picture.fibonacci.zone_label,
                retracements=picture.fibonacci.retracements,
                extensions=picture.fibonacci.extensions,
            ),
            levels=_levels(picture.levels, ind.atr),
            indicators=IndicatorsModel(
                price=ind.price,
                rsi=ind.rsi,
                atr=ind.atr,
                atr_pct=ind.atr_pct,
                atr_average_pct=ind.atr_average_pct,
                ema_fast=ind.ema_fast,
                ema_slow=ind.ema_slow,
                adx=ind.adx,
                plus_di=ind.plus_di,
                minus_di=ind.minus_di,
                bb_upper=ind.bb_upper,
                bb_middle=ind.bb_middle,
                bb_lower=ind.bb_lower,
                bb_bandwidth=ind.bb_bandwidth,
                volume=ind.volume,
                volume_average=ind.volume_average,
                volume_ratio=ind.volume_ratio,
            ),
            regime=RegimeModel(
                direction=picture.regime.direction.value,
                volatility=picture.regime.volatility.value,
                adx=picture.regime.adx,
                tradeable=picture.regime.tradeable,
                reason=picture.regime.reason,
            ),
            confluence=ConfluenceModel(
                direction=confluence.direction,
                agreement=confluence.agreement,
                bullish_weight=confluence.bullish_weight,
                bearish_weight=confluence.bearish_weight,
                timeframe_agreement=confluence.timeframe_agreement,
                conflicts=confluence.conflicts,
                hard_conflicts=confluence.hard_conflicts,
                reason=confluence.reason,
            ),
            order_blocks=_order_blocks(picture.order_blocks),
            fair_value_gaps=_fair_value_gaps(picture.fair_value_gaps),
            liquidity=_liquidity(picture.liquidity),
            vwap=_vwap(picture.vwap),
            macd=_macd(picture.macd),
            adx=_adx(picture.adx),
            patterns=_patterns(picture.patterns),
            market_quality=[
                MarketQualityModel(name=f.name, passed=f.passed, detail=f.detail)
                for f in picture.quality_filters
            ],
            market_conditions_clean=picture.market_conditions_clean,
            context=MarketContextModel(
                benchmark_symbol=picture.benchmark_symbol,
                benchmark_trend=picture.benchmark_regime.value,
                last_price=quote.last if quote else ind.price,
                bid=quote.bid if quote else None,
                ask=quote.ask if quote else None,
                spread_pct=quote.spread_pct if quote else None,
                quote_volume_24h=quote.quote_volume_24h if quote else None,
            ),
            candles_analyzed=len(picture.candles),
            timeframes_analyzed=len(mtf.views),
        ),
        quality_detail=_score_detail(result.quality),
        confidence_detail=_score_detail(result.confidence),
        intelligence=_intelligence(result.intelligence),

        generated_at=datetime.now(timezone.utc).isoformat(),
        elapsed_ms=result.elapsed_ms,
    )
