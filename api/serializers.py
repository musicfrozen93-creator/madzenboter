"""Translate pipeline dataclasses into the API response models.

Keeping this separate means the pipeline never imports Pydantic and the API
never reaches into pipeline internals.

Only the SELECTED timeframe is serialized. The internal ladder rungs informed the
result but are not exposed — the user asked about one timeframe and gets one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from analysis.diagnostic import build_diagnostic
from analysis.elliott import ElliottState, WaveCount
from analysis.levels import SupportResistance, Zone
from analysis.modules import (
    ICT_BOS,
    ICT_CONFLUENCE,
    ICT_DISPLACEMENT,
    ICT_FVG,
    ICT_LIQUIDITY,
    ICT_LIQUIDITY_SWEEPS,
    ICT_MSS,
    ICT_ORDER_BLOCKS,
    ICT_PREMIUM_DISCOUNT,
    MSNR_KEY_SR,
    MSNR_LOCATION,
    MSNR_PDHL,
    MSNR_PWHL,
    MSNR_RESISTANCE_ZONES,
    MSNR_SUPPORT_ZONES,
    MSNR_SWING_LEVELS,
    ict_enabled,
    msnr_enabled,
)
from analysis.pipeline import AnalysisResult
from analysis.scoring import Score
from api.schemas import (
    ADXTrendModel,
    AnalysisDetailModel,
    AnalyzeResponse,
    ConfluenceModel,
    DisplacementModel,
    ElliottModel,
    FairValueGapModel,
    FibonacciModel,
    GuideStepModel,
    HealthModel,
    ICTStructureModel,
    IctMsnrConfluenceModel,
    IndicatorsModel,
    IntelligenceModel,
    InvalidationConditionModel,
    InvalidationModel,
    LevelsModel,
    LifecycleModel,
    LiquidityModel,
    MACDModel,
    MSNRModel,
    MarketContextModel,
    MarketQualityModel,
    ModuleBreakdownModel,
    NarrativeModel,
    OrderBlockModel,
    PatternModel,
    PremiumDiscountModel,
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


def _msnr(m) -> MSNRModel:
    return MSNRModel(
        location=m.location,
        nearest_support=m.nearest_support,
        nearest_resistance=m.nearest_resistance,
        distance_to_support=m.distance_to_support,
        distance_to_resistance=m.distance_to_resistance,
        strength=round(m.strength, 4),
        score=round(m.score, 4),
        explanation=m.explanation,
    )


def _displacement(d) -> DisplacementModel:
    return DisplacementModel(
        direction=d.direction,
        strength=round(d.strength, 4),
        has_structure_break=d.has_structure_break,
        explanation=d.explanation,
    )


def _premium_discount(pd) -> PremiumDiscountModel:
    return PremiumDiscountModel(
        zone=pd.zone,
        range_high=pd.range_high,
        range_low=pd.range_low,
        midpoint=pd.midpoint,
        price_position=round(pd.price_position, 4),
        explanation=pd.explanation,
    )


def _ict_structure(ict) -> ICTStructureModel:
    return ICTStructureModel(
        has_bos=ict.has_bos,
        bos_direction=ict.bos_direction,
        has_mss=ict.has_mss,
        mss_direction=ict.mss_direction,
        higher_highs=ict.higher_highs,
        higher_lows=ict.higher_lows,
        lower_highs=ict.lower_highs,
        lower_lows=ict.lower_lows,
        trend=ict.trend,
        strength=round(ict.strength, 4),
        explanation=ict.explanation,
    )


def _ict_msnr_confluence(c) -> IctMsnrConfluenceModel:
    return IctMsnrConfluenceModel(
        direction=c.direction,
        strength=round(c.strength, 4),
        score=round(c.score, 4),
        bullish_elements=[f'{e.name}: {e.detail}' for e in c.bullish_elements],
        bearish_elements=[f'{e.name}: {e.detail}' for e in c.bearish_elements],
        neutral_elements=[f'{e.name}: {e.detail}' for e in c.neutral_elements],
        conflicts=c.conflicts,
        primary_reason=c.primary_reason,
        explanation=c.explanation,
        primary_blocker=c.primary_blocker,
        secondary_blockers=c.secondary_blockers,
        evidence_count=c.evidence_count,
        deduplicated_count=c.deduplicated_count,
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


def _bias_from_direction(direction: str) -> str:
    """Map an internal direction to the frontend's bias string."""
    d = (direction or '').lower()
    if d == 'bullish':
        return 'bullish'
    if d == 'bearish':
        return 'bearish'
    return 'neutral'


def _strength_bucket(strength: float) -> str:
    """Bucket a 0–1 strength into the strong/moderate/weak labels the UI uses."""
    if strength >= 0.65:
        return 'strong'
    if strength >= 0.35:
        return 'moderate'
    return 'weak'


def _pass(flag: bool) -> str:
    return 'PASS' if flag else 'FAIL'


def _build_ict_analysis(picture, enabled: frozenset) -> Optional[dict]:
    """Build the top-level `ict_analysis` panel the frontend renders.

    Every ICT toggle that is OFF is omitted from the block so a disabled
    module never shows as active evidence. When every ICT toggle is off the
    entire panel is returned as None and the UI drops the panel.
    """
    if not ict_enabled(enabled):
        return None

    liq = picture.liquidity
    fvg = picture.fair_value_gaps
    ob = picture.order_blocks
    ict_struct = picture.ict_structure
    disp = picture.displacement
    pd_res = picture.premium_discount
    conf = picture.ict_confluence

    sweep = liq.last_sweep

    liquidity = None
    if ICT_LIQUIDITY in enabled:
        liquidity = {
            'direction': liq.direction,
            'detected': liq.direction in ('bullish', 'bearish'),
            'passed': liq.direction in ('bullish', 'bearish'),
            'label': liq.reason,
        }

    liquidity_sweeps = None
    if ICT_LIQUIDITY_SWEEPS in enabled:
        liquidity_sweeps = {
            'detected': sweep is not None,
            'passed': sweep is not None,
            'side': sweep.side if sweep else None,
            'grabbed': bool(sweep.grabbed) if sweep else False,
            'label': (
                f"{sweep.side.replace('_', '-')} liquidity swept" if sweep
                else 'no recent sweep'
            ),
        }

    mss = None
    if ICT_MSS in enabled:
        mss = {
            'detected': ict_struct.has_mss,
            'passed': ict_struct.has_mss,
            'direction': ict_struct.mss_direction,
            'label': (
                f"{(ict_struct.mss_direction or '').title()} MSS"
                if ict_struct.has_mss else 'no MSS'
            ),
        }

    bos = None
    if ICT_BOS in enabled:
        bos = {
            'detected': ict_struct.has_bos,
            'passed': ict_struct.has_bos,
            'direction': ict_struct.bos_direction,
            'label': (
                f"{(ict_struct.bos_direction or '').title()} BOS"
                if ict_struct.has_bos else 'no BOS'
            ),
        }

    displacement = None
    if ICT_DISPLACEMENT in enabled:
        d_active = disp.direction in ('bullish', 'BULLISH', 'bearish', 'BEARISH')
        displacement = {
            'detected': d_active,
            'passed': d_active,
            'direction': disp.direction.lower() if disp.direction else None,
            'strength': round(disp.strength, 3),
            'label': disp.explanation,
        }

    fvg_block = None
    if ICT_FVG in enabled:
        fvg_block = {
            'detected': fvg.nearest is not None,
            'passed': fvg.direction in ('bullish', 'bearish'),
            'direction': fvg.direction,
            'count': fvg.unfilled_count if hasattr(fvg, 'unfilled_count') else len(fvg.unfilled),
            'label': fvg.reason,
        }

    order_blocks_block = None
    if ICT_ORDER_BLOCKS in enabled:
        order_blocks_block = {
            'detected': ob.nearest is not None,
            'passed': ob.direction in ('bullish', 'bearish'),
            'direction': ob.direction,
            'label': ob.reason,
        }

    premium_discount = None
    if ICT_PREMIUM_DISCOUNT in enabled:
        zone = (pd_res.zone or '').lower()
        premium_discount = {
            'passed': zone in ('premium', 'discount'),
            'zone': zone if zone in ('premium', 'discount', 'equilibrium') else None,
            'level': round(pd_res.price_position, 4),
            'label': pd_res.explanation,
        }

    rows = [r for r in (
        liquidity_sweeps, liquidity, mss, bos, displacement,
        fvg_block, order_blocks_block, premium_discount,
    ) if r is not None]
    pass_count = sum(1 for r in rows if r.get('passed'))

    # Diagnostics — one PASS/FAIL line per enabled ICT toggle.
    diagnostics = []
    for label, block in (
        ('Liquidity', liquidity),
        ('Liquidity Sweep', liquidity_sweeps),
        ('MSS', mss),
        ('BOS', bos),
        ('Displacement', displacement),
        ('FVG', fvg_block),
        ('Order Block', order_blocks_block),
        ('Premium/Discount', premium_discount),
    ):
        if block is None:
            continue
        diagnostics.append({
            'key': label.lower().replace(' ', '_').replace('/', '_'),
            'label': label,
            'passed': bool(block.get('passed')),
            'status': _pass(bool(block.get('passed'))),
        })

    bias = _bias_from_direction(conf.direction) if ICT_CONFLUENCE in enabled else 'neutral'

    return {
        'liquidity': liquidity,
        'liquidity_sweeps': liquidity_sweeps,
        'mss': mss,
        'bos': bos,
        'displacement': displacement,
        'fvg': fvg_block,
        'order_blocks': order_blocks_block,
        'premium_discount': premium_discount,
        'bias': bias,
        'confluence_score': pass_count,
        'confluence_total': len(rows),
        'diagnostics': diagnostics,
    }


def _build_msnr_analysis(picture, enabled: frozenset) -> Optional[dict]:
    """Build the top-level `msnr_analysis` panel the frontend renders.

    Same rule as ICT: any MSNR toggle that is OFF is omitted from the block
    (its key is set to None or the list stays empty), and when the entire
    category is off the whole panel is None.
    """
    if not msnr_enabled(enabled):
        return None

    m = picture.msnr
    levels = picture.levels

    nearest_support = (
        m.nearest_support if MSNR_SUPPORT_ZONES in enabled else None
    )
    nearest_resistance = (
        m.nearest_resistance if MSNR_RESISTANCE_ZONES in enabled else None
    )
    support_strength = (
        _strength_bucket(levels.nearest_support.strength)
        if (MSNR_SUPPORT_ZONES in enabled and levels.nearest_support) else None
    )
    resistance_strength = (
        _strength_bucket(levels.nearest_resistance.strength)
        if (MSNR_RESISTANCE_ZONES in enabled and levels.nearest_resistance) else None
    )

    # Location classification only appears when the location toggle is on.
    if MSNR_LOCATION in enabled:
        current_location = (m.location or '').lower()
    else:
        current_location = None

    # PDH/PDL and PWH/PWL come from the MSNR engine's periodic_levels list.
    pdh = pdl = pwh = pwl = None
    for p in m.periodic_levels:
        if p.label == 'PDH' and MSNR_PDHL in enabled:
            pdh = p.price
        elif p.label == 'PDL' and MSNR_PDHL in enabled:
            pdl = p.price
        elif p.label == 'PWH' and MSNR_PWHL in enabled:
            pwh = p.price
        elif p.label == 'PWL' and MSNR_PWHL in enabled:
            pwl = p.price

    swing_levels = []
    if MSNR_SWING_LEVELS in enabled:
        for price in m.important_swing_highs[:5]:
            swing_levels.append({'type': 'swing_high', 'price': price})
        for price in m.important_swing_lows[:5]:
            swing_levels.append({'type': 'swing_low', 'price': price})

    key_sr_zones = []
    if MSNR_KEY_SR in enabled:
        for price in m.repeated_levels[:6]:
            key_sr_zones.append({'type': 'zone', 'price': price, 'strength': 'strong'})

    # Bias derived from location — only when location toggle is enabled.
    bias = 'neutral'
    if MSNR_LOCATION in enabled:
        loc = (m.location or '').upper()
        if loc == 'SUPPORT_ZONE':
            bias = 'bullish'
        elif loc == 'RESISTANCE_ZONE':
            bias = 'bearish'

    if MSNR_LOCATION in enabled:
        if m.strength >= 0.65:
            location_quality = 'high'
        elif m.strength >= 0.35:
            location_quality = 'medium'
        else:
            location_quality = 'low'
    else:
        location_quality = None

    diagnostics = []
    if MSNR_SUPPORT_ZONES in enabled:
        diagnostics.append({
            'key': 'support_zones', 'label': 'Support Zones',
            'passed': nearest_support is not None,
            'status': _pass(nearest_support is not None),
        })
    if MSNR_RESISTANCE_ZONES in enabled:
        diagnostics.append({
            'key': 'resistance_zones', 'label': 'Resistance Zones',
            'passed': nearest_resistance is not None,
            'status': _pass(nearest_resistance is not None),
        })
    if MSNR_PDHL in enabled:
        diagnostics.append({
            'key': 'pdhl', 'label': 'Previous Day H/L',
            'passed': pdh is not None or pdl is not None,
            'status': _pass(pdh is not None or pdl is not None),
        })
    if MSNR_PWHL in enabled:
        diagnostics.append({
            'key': 'pwhl', 'label': 'Previous Week H/L',
            'passed': pwh is not None or pwl is not None,
            'status': _pass(pwh is not None or pwl is not None),
        })
    if MSNR_SWING_LEVELS in enabled:
        diagnostics.append({
            'key': 'swing_levels', 'label': 'Swing Levels',
            'passed': bool(swing_levels),
            'status': _pass(bool(swing_levels)),
        })
    if MSNR_KEY_SR in enabled:
        diagnostics.append({
            'key': 'key_sr', 'label': 'Key S/R Zones',
            'passed': bool(key_sr_zones),
            'status': _pass(bool(key_sr_zones)),
        })
    if MSNR_LOCATION in enabled:
        diagnostics.append({
            'key': 'location', 'label': 'Location',
            'passed': (m.location or '').upper() in ('SUPPORT_ZONE', 'RESISTANCE_ZONE'),
            'status': _pass(
                (m.location or '').upper() in ('SUPPORT_ZONE', 'RESISTANCE_ZONE')
            ),
        })

    return {
        'nearest_support': nearest_support,
        'nearest_resistance': nearest_resistance,
        'current_location': current_location,
        'support_strength': support_strength,
        'resistance_strength': resistance_strength,
        'pdh': pdh, 'pdl': pdl, 'pwh': pwh, 'pwl': pwl,
        'swing_levels': swing_levels,
        'key_sr_zones': key_sr_zones,
        'bias': bias,
        'location_quality': location_quality,
        'diagnostics': diagnostics,
    }


def _build_ict_confluence_panel(picture, enabled: frozenset) -> Optional[dict]:
    """Build the final ICT+MSNR confluence panel.

    Present only when the ict_confluence toggle is on — otherwise the panel is
    null so nothing about the confluence appears anywhere in the response.
    """
    if ICT_CONFLUENCE not in enabled:
        return None

    conf = picture.ict_confluence
    direction = (conf.direction or 'range').lower()
    if direction == 'range':
        strength_label = 'weak'
    else:
        strength_label = _strength_bucket(conf.strength)

    return {
        'direction': direction,
        'bias': _bias_from_direction(direction),
        'strength': round(conf.strength, 4),
        'strength_label': strength_label,
        'score': round(conf.score, 4),
        'bullish_elements': [
            {'name': e.name, 'detail': e.detail, 'strength': round(e.strength, 4)}
            for e in conf.bullish_elements
        ],
        'bearish_elements': [
            {'name': e.name, 'detail': e.detail, 'strength': round(e.strength, 4)}
            for e in conf.bearish_elements
        ],
        'conflicts': list(conf.conflicts),
        'primary_reason': conf.primary_reason,
        'primary_blocker': conf.primary_blocker,
        'secondary_blockers': list(conf.secondary_blockers),
        'explanation': conf.explanation,
        'evidence_count': conf.evidence_count,
        'deduplicated_count': conf.deduplicated_count,
    }


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
        enabled_indicators=sorted(confluence.enabled_modules),

        entry=signal.entry,
        sl=signal.stop_loss,
        tp=list(signal.take_profits),
        risk_reward=signal.risk_reward,
        risk_pct=signal.risk_pct,
        rr_per_tp=list(signal.rr_per_tp),

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
                ict_msnr_direction=confluence.ict_msnr_direction,
                ict_msnr_strength=confluence.ict_msnr_strength,
                ict_msnr_score=confluence.ict_msnr_score,
            ),
            order_blocks=_order_blocks(picture.order_blocks),
            fair_value_gaps=_fair_value_gaps(picture.fair_value_gaps),
            liquidity=_liquidity(picture.liquidity),
            vwap=_vwap(picture.vwap),
            macd=_macd(picture.macd),
            adx=_adx(picture.adx),
            patterns=_patterns(picture.patterns),
            msnr=_msnr(picture.msnr),
            displacement=_displacement(picture.displacement),
            premium_discount=_premium_discount(picture.premium_discount),
            ict_structure=_ict_structure(picture.ict_structure),
            ict_msnr_confluence=_ict_msnr_confluence(picture.ict_confluence),
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

        ict_analysis=_build_ict_analysis(picture, confluence.enabled_modules),
        msnr_analysis=_build_msnr_analysis(picture, confluence.enabled_modules),
        ict_confluence=_build_ict_confluence_panel(picture, confluence.enabled_modules),

        diagnostic=_diagnostic(result),

        generated_at=datetime.now(timezone.utc).isoformat(),
        elapsed_ms=result.elapsed_ms,
    )


def _diagnostic(result: AnalysisResult) -> dict:
    """Build the diagnostic breakdown dict for the API response."""
    diag = build_diagnostic(
        result.signal, result.confluence, result.quality, result.confidence,
    )
    return diag.as_dict()
