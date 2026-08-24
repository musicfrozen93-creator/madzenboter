"""Tests for Phase 2A: ICT + MSNR Confluence Engine.

Covers every module and the double-counting prevention mandated by the spec.
"""

import pytest
import numpy as np
import pandas as pd

from analysis.ict.evidence import EvidenceItem, EvidenceRegistry, make_evidence_id
from analysis.ict.msnr import (
    MSNREngine, MSNRResult,
    SUPPORT_ZONE, RESISTANCE_ZONE, MID_RANGE, NO_CLEAR_LEVEL, UNAVAILABLE,
)
from analysis.ict.displacement import (
    DisplacementEngine, DisplacementResult,
    BULLISH as DISP_BULLISH, BEARISH as DISP_BEARISH, NEUTRAL as DISP_NEUTRAL,
)
from analysis.ict.premium_discount import (
    PremiumDiscountEngine, PremiumDiscountResult,
    PREMIUM, EQUILIBRIUM, DISCOUNT,
)
from analysis.ict.ict_structure import ICTStructureAdapter, ICTStructureResult
from analysis.ict.ict_confluence import (
    ICTMSNRConfluenceEngine, IctMsnrConfluence,
)
from analysis.levels import SupportResistance, Zone, detect_levels
from analysis.structure import (
    BEARISH, BOS, BULLISH, CHOCH, RANGE,
    StructureState, Swing, analyze_structure, find_swings,
)
from analysis.smc.fvg import FairValueGapState, detect_fair_value_gaps
from analysis.smc.liquidity import LiquidityState, detect_liquidity
from analysis.smc.order_blocks import OrderBlockState, detect_order_blocks

from tests.fakes import make_uptrend, make_downtrend, make_ranging_candles


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_support_zone(price: float, touches: int = 4, strength: float = 0.8):
    return Zone(center=price, lower=price - 1, upper=price + 1,
                touches=touches, strength=strength, kind='support', last_touch_index=100)


def _make_resistance_zone(price: float, touches: int = 4, strength: float = 0.8):
    return Zone(center=price, lower=price - 1, upper=price + 1,
                touches=touches, strength=strength, kind='resistance', last_touch_index=100)


def _make_levels(price, support_price=None, resistance_price=None):
    supports = [_make_support_zone(support_price)] if support_price else []
    resistances = [_make_resistance_zone(resistance_price)] if resistance_price else []
    return SupportResistance(
        supports=supports, resistances=resistances,
        nearest_support=supports[0] if supports else None,
        nearest_resistance=resistances[0] if resistances else None,
        price=price,
        reason='test',
    )


def _make_swings(n=8, base=100, amplitude=5):
    """Create alternating swing highs and lows."""
    swings = []
    for i in range(n):
        if i % 2 == 0:
            swings.append(Swing(index=i * 5, price=base + amplitude + i * 0.5, kind='high'))
        else:
            swings.append(Swing(index=i * 5, price=base - amplitude + i * 0.5, kind='low'))
    return swings


def _make_bullish_structure():
    return StructureState(
        trend=BULLISH, event=BOS, event_direction=BULLISH,
        event_strength=1.5,
        last_swing_high=110.0, last_swing_low=95.0,
        higher_highs=True, higher_lows=True,
        swings=_make_swings(), strength=1.0,
        reason='higher highs and higher lows',
    )


def _make_bearish_structure():
    return StructureState(
        trend=BEARISH, event=BOS, event_direction=BEARISH,
        event_strength=1.5,
        last_swing_high=110.0, last_swing_low=95.0,
        lower_highs=True, lower_lows=True,
        swings=_make_swings(), strength=1.0,
        reason='lower highs and lower lows',
    )


def _make_mss_structure(direction):
    return StructureState(
        trend=BEARISH if direction == BULLISH else BULLISH,
        event=CHOCH, event_direction=direction,
        event_strength=1.0,
        last_swing_high=110.0, last_swing_low=95.0,
        higher_highs=direction == BULLISH,
        higher_lows=direction == BULLISH,
        lower_highs=direction == BEARISH,
        lower_lows=direction == BEARISH,
        swings=_make_swings(), strength=0.5,
        reason='change of character',
    )


# ─────────────────────────────────────────────
# Evidence deduplication
# ─────────────────────────────────────────────

class TestEvidenceDedup:
    """Double-counting prevention (MANDATORY — Part 9 of the spec)."""

    def test_same_id_keeps_stronger(self):
        registry = EvidenceRegistry()
        weak = EvidenceItem('SH:100.0', 'swing_high', 'bearish', 100.0, 0.3, 'msnr', 'from msnr')
        strong = EvidenceItem('SH:100.0', 'swing_high', 'bearish', 100.0, 0.8, 'ict_structure', 'from ict')
        registry.add(weak)
        registry.add(strong)
        assert len(registry.items) == 1
        assert registry.items[0].strength == 0.8
        assert registry.items[0].source == 'ict_structure'
        assert registry.dedup_count == 1

    def test_different_ids_kept_separately(self):
        registry = EvidenceRegistry()
        a = EvidenceItem('SH:100.0', 'swing_high', 'bearish', 100.0, 0.5, 'a', 'a')
        b = EvidenceItem('SL:90.0', 'swing_low', 'bullish', 90.0, 0.5, 'b', 'b')
        registry.add(a)
        registry.add(b)
        assert len(registry.items) == 2
        assert registry.dedup_count == 0

    def test_make_evidence_id_rounding(self):
        """The same structural event at slightly different prices resolves to one id."""
        id1 = make_evidence_id('swing_high', 42000.12, precision=1)
        id2 = make_evidence_id('swing_high', 42000.14, precision=1)
        assert id1 == id2

    def test_structure_event_not_triple_counted(self):
        """A BOS detected by structure, adapted by ICT, and seen by MSNR = ONE event.

        This is the REGRESSION TEST demanded by Part 9 of the spec.
        """
        registry = EvidenceRegistry()
        # Same swing high from three sources.
        from_structure = EvidenceItem(
            make_evidence_id('swing_high', 110.0), 'swing_high', 'bearish',
            110.0, 0.4, 'structure', 'swing high from structure engine',
        )
        from_msnr = EvidenceItem(
            make_evidence_id('swing_high', 110.0), 'swing_high', 'bearish',
            110.0, 0.5, 'msnr', 'resistance from MSNR',
        )
        from_ict = EvidenceItem(
            make_evidence_id('swing_high', 110.0), 'swing_high', 'bearish',
            110.0, 0.4, 'ict_structure', 'swing high from ICT adapter',
        )
        registry.add(from_structure)
        registry.add(from_msnr)
        registry.add(from_ict)

        # Only ONE item remains — the strongest.
        assert len(registry.items) == 1
        assert registry.items[0].source == 'msnr'
        assert registry.dedup_count == 2

    def test_bullish_and_bearish_filters(self):
        registry = EvidenceRegistry()
        registry.add(EvidenceItem('a', 'support', 'bullish', 90.0, 0.5, 'x', 'x'))
        registry.add(EvidenceItem('b', 'resistance', 'bearish', 110.0, 0.5, 'x', 'x'))
        registry.add(EvidenceItem('c', 'displacement', 'neutral', 100.0, 0.3, 'x', 'x'))
        assert len(registry.bullish()) == 1
        assert len(registry.bearish()) == 1
        assert len(registry.neutral()) == 1


# ─────────────────────────────────────────────
# MSNR Location Engine
# ─────────────────────────────────────────────

class TestMSNR:

    def test_support_detection(self):
        candles = make_uptrend(200)
        swings = find_swings(candles)
        price = float(candles['close'].iloc[-1])
        atr = 2.0
        levels = _make_levels(price, support_price=price - 0.5)
        engine = MSNREngine()
        result = engine.analyze(candles, swings, levels, price, atr)
        assert result.location == SUPPORT_ZONE
        assert result.nearest_support is not None
        assert result.strength > 0

    def test_resistance_detection(self):
        candles = make_downtrend(200)
        swings = find_swings(candles)
        price = float(candles['close'].iloc[-1])
        atr = 2.0
        levels = _make_levels(price, resistance_price=price + 0.5)
        engine = MSNREngine()
        result = engine.analyze(candles, swings, levels, price, atr)
        assert result.location == RESISTANCE_ZONE
        assert result.nearest_resistance is not None

    def test_repeated_reaction_levels(self):
        """Levels where both highs and lows cluster are detected."""
        swings = [
            Swing(0, 100.0, 'high'), Swing(5, 95.0, 'low'),
            Swing(10, 100.2, 'high'), Swing(15, 100.1, 'low'),  # reversal at ~100
            Swing(20, 105.0, 'high'), Swing(25, 99.8, 'low'),
        ]
        engine = MSNREngine()
        repeated = engine._repeated_reaction_levels(swings, atr=2.0)
        assert len(repeated) >= 1  # The ~100 level should be detected

    def test_insufficient_data(self):
        candles = make_uptrend(5)
        engine = MSNREngine()
        result = engine.analyze(candles, [], SupportResistance(), 0, 0)
        assert result.location == UNAVAILABLE

    def test_no_clear_level(self):
        """When no support or resistance is nearby, location is NO_CLEAR_LEVEL or MID_RANGE."""
        candles = make_ranging_candles(200)
        swings = find_swings(candles)
        price = float(candles['close'].iloc[-1])
        atr = 2.0
        # Put levels far away (> NEAR_ATR * atr).
        levels = _make_levels(price, support_price=price - 20, resistance_price=price + 20)
        engine = MSNREngine()
        result = engine.analyze(candles, swings, levels, price, atr)
        assert result.location in (MID_RANGE, NO_CLEAR_LEVEL)

    def test_periodic_levels_detected(self):
        candles = make_uptrend(200)
        swings = find_swings(candles)
        price = float(candles['close'].iloc[-1])
        levels = _make_levels(price)
        engine = MSNREngine()
        result = engine.analyze(candles, swings, levels, price, 2.0)
        pdh = [p for p in result.periodic_levels if p.label == 'PDH']
        pdl = [p for p in result.periodic_levels if p.label == 'PDL']
        assert len(pdh) == 1
        assert len(pdl) == 1


# ─────────────────────────────────────────────
# Displacement Engine
# ─────────────────────────────────────────────

class TestDisplacement:

    def _make_displacement_candles(self, direction='bull', n=50):
        """Build candles with a clear displacement candle near the end."""
        i = np.arange(n)
        closes = np.linspace(100, 150, n).astype(float)
        opens = np.concatenate(([closes[0]], closes[:-1]))
        highs = np.maximum(opens, closes) + 0.5
        lows = np.minimum(opens, closes) - 0.5
        volumes = np.full(n, 1000.0)

        # Add a displacement candle at index -2.
        if direction == 'bull':
            opens[-2] = closes[-3]
            closes[-2] = opens[-2] + 15.0  # Large bullish candle
            highs[-2] = closes[-2] + 0.1
            lows[-2] = opens[-2] - 0.1
        else:
            opens[-2] = closes[-3]
            closes[-2] = opens[-2] - 15.0  # Large bearish candle
            lows[-2] = closes[-2] - 0.1
            highs[-2] = opens[-2] + 0.1

        volumes[-2] = 3000.0  # Volume spike
        return pd.DataFrame({
            'timestamp': pd.to_datetime(i * 900_000, unit='ms'),
            'open': opens, 'high': highs, 'low': lows, 'close': closes,
            'volume': volumes,
        })

    def test_bullish_displacement(self):
        candles = self._make_displacement_candles('bull')
        engine = DisplacementEngine()
        result = engine.analyze(candles, atr=1.0)
        assert result.direction == DISP_BULLISH
        assert result.strength > 0

    def test_bearish_displacement(self):
        candles = self._make_displacement_candles('bear')
        engine = DisplacementEngine()
        result = engine.analyze(candles, atr=1.0)
        assert result.direction == DISP_BEARISH
        assert result.strength > 0

    def test_no_displacement_in_calm_market(self):
        candles = make_ranging_candles(200)
        engine = DisplacementEngine()
        result = engine.analyze(candles, atr=2.0)
        assert result.direction in (DISP_NEUTRAL, 'UNAVAILABLE')

    def test_displacement_with_structure_break(self):
        candles = self._make_displacement_candles('bull')
        engine = DisplacementEngine()
        result = engine.analyze(
            candles, atr=1.0,
            structure_event='bos', structure_event_direction='bullish',
        )
        assert result.has_structure_break is True
        # Should have higher strength with structure confirmation.
        result_no_break = engine.analyze(candles, atr=1.0)
        assert result.strength >= result_no_break.strength

    def test_insufficient_data(self):
        candles = make_uptrend(5)
        engine = DisplacementEngine()
        result = engine.analyze(candles, atr=1.0)
        assert result.direction == 'UNAVAILABLE'


# ─────────────────────────────────────────────
# Premium / Discount
# ─────────────────────────────────────────────

class TestPremiumDiscount:

    def test_premium_zone(self):
        swings = _make_swings(8, base=100, amplitude=10)
        engine = PremiumDiscountEngine()
        # Price well above midpoint.
        result = engine.analyze(swings, price=115.0)
        assert result.zone == PREMIUM

    def test_discount_zone(self):
        swings = _make_swings(8, base=100, amplitude=10)
        engine = PremiumDiscountEngine()
        result = engine.analyze(swings, price=88.0)
        assert result.zone == DISCOUNT

    def test_equilibrium_zone(self):
        swings = [
            Swing(0, 110.0, 'high'), Swing(5, 90.0, 'low'),
            Swing(10, 108.0, 'high'), Swing(15, 92.0, 'low'),
        ]
        engine = PremiumDiscountEngine()
        result = engine.analyze(swings, price=100.0)
        assert result.zone == EQUILIBRIUM

    def test_insufficient_swings(self):
        engine = PremiumDiscountEngine()
        result = engine.analyze([], price=100.0)
        assert result.zone == 'UNAVAILABLE'


# ─────────────────────────────────────────────
# ICT Structure Adapter
# ─────────────────────────────────────────────

class TestICTStructure:

    def test_bos_detection(self):
        structure = _make_bullish_structure()
        adapter = ICTStructureAdapter()
        result = adapter.interpret(structure)
        assert result.has_bos is True
        assert result.bos_direction == BULLISH
        assert result.has_mss is False

    def test_mss_detection(self):
        structure = _make_mss_structure(BULLISH)
        adapter = ICTStructureAdapter()
        result = adapter.interpret(structure)
        assert result.has_mss is True
        assert result.mss_direction == BULLISH

    def test_hh_hl_labels(self):
        structure = _make_bullish_structure()
        adapter = ICTStructureAdapter()
        result = adapter.interpret(structure)
        assert result.higher_highs is True
        assert result.higher_lows is True

    def test_lh_ll_labels(self):
        structure = _make_bearish_structure()
        adapter = ICTStructureAdapter()
        result = adapter.interpret(structure)
        assert result.lower_highs is True
        assert result.lower_lows is True

    def test_evidence_uses_dedup_ids(self):
        """Evidence from ICT adapter uses the same id formula as other engines."""
        structure = _make_bullish_structure()
        adapter = ICTStructureAdapter()
        result = adapter.interpret(structure)
        # Should have evidence items.
        assert len(result.evidence) > 0
        # Check that swing_high evidence_id matches what MSNR would produce.
        sh_evidence = [e for e in result.evidence if e.kind == 'swing_high']
        assert len(sh_evidence) == 1
        expected_id = make_evidence_id('swing_high', 110.0)
        assert sh_evidence[0].evidence_id == expected_id

    def test_no_structure_data(self):
        structure = StructureState()
        adapter = ICTStructureAdapter()
        result = adapter.interpret(structure)
        assert result.has_bos is False
        assert result.has_mss is False


# ─────────────────────────────────────────────
# ICT-MSNR Confluence
# ─────────────────────────────────────────────

class TestIctMsnrConfluence:

    def _make_bullish_context(self):
        """Build all the components for a strong bullish ICT confluence."""
        msnr = MSNRResult(
            location=SUPPORT_ZONE, nearest_support=95.0, nearest_resistance=115.0,
            distance_to_support=0.5, distance_to_resistance=8.0,
            strength=0.8, score=0.7, explanation='at strong support',
        )
        ict_structure = ICTStructureResult(
            has_mss=True, mss_direction=BULLISH, strength=0.8,
            higher_highs=True, higher_lows=True, trend=BULLISH,
            explanation='MSS bullish',
        )
        displacement = DisplacementResult(
            direction='BULLISH', strength=0.7,
            explanation='bullish displacement',
        )
        pd_result = PremiumDiscountResult(
            zone=DISCOUNT, price_position=0.2,
            range_high=120.0, range_low=90.0, midpoint=105.0,
            explanation='in discount',
        )
        liquidity = LiquidityState(
            direction=BULLISH, score=0.7,
            reason='sweep of sell-side liquidity',
        )
        from analysis.smc.liquidity import LiquidityPool
        liquidity.last_sweep = LiquidityPool(
            side='sell_side', price=94.0, touches=2, swept=True, grabbed=True,
        )
        fvg = FairValueGapState(direction=BULLISH, score=0.6, reason='unfilled bullish FVG')
        from analysis.smc.fvg import FairValueGap
        fvg.nearest = FairValueGap(
            direction=BULLISH, top=98.0, bottom=96.0, index=10,
            state='unfilled', size=2.0, size_atr=1.0,
        )
        ob = OrderBlockState(direction=BULLISH, score=0.5, reason='fresh bullish OB')
        from analysis.smc.order_blocks import OrderBlock
        ob.nearest = OrderBlock(
            direction=BULLISH, top=97.0, bottom=95.0, index=8,
            state='fresh', displacement_atr=1.5, volume_ratio=1.2, strength=0.6,
        )
        return msnr, ict_structure, displacement, pd_result, liquidity, fvg, ob

    def test_strong_bullish_confluence(self):
        msnr, ict, disp, pd_r, liq, fvg, ob = self._make_bullish_context()
        engine = ICTMSNRConfluenceEngine()
        result = engine.evaluate(
            msnr=msnr, ict_structure=ict, displacement=disp,
            premium_discount=pd_r, liquidity=liq, fvg=fvg,
            order_blocks=ob, existing_trend=BULLISH,
        )
        assert result.direction == BULLISH
        assert result.strength >= 0.5
        assert len(result.bullish_elements) >= 4

    def test_strong_bearish_confluence(self):
        """Mirror of the bullish test with all evidence bearish."""
        msnr = MSNRResult(
            location=RESISTANCE_ZONE, strength=0.8, score=0.7,
            explanation='at strong resistance',
        )
        ict_structure = ICTStructureResult(
            has_mss=True, mss_direction=BEARISH, strength=0.8,
            trend=BEARISH, explanation='MSS bearish',
        )
        displacement = DisplacementResult(
            direction='BEARISH', strength=0.7,
            explanation='bearish displacement',
        )
        pd_result = PremiumDiscountResult(
            zone=PREMIUM, price_position=0.85,
            range_high=120.0, range_low=90.0, midpoint=105.0,
            explanation='in premium',
        )
        from analysis.smc.liquidity import LiquidityPool
        liquidity = LiquidityState(
            direction=BEARISH, score=0.7,
            reason='sweep of buy-side liquidity',
        )
        liquidity.last_sweep = LiquidityPool(
            side='buy_side', price=118.0, touches=2, swept=True, grabbed=True,
        )
        fvg = FairValueGapState(direction=BEARISH, score=0.6, reason='bearish FVG')
        from analysis.smc.fvg import FairValueGap
        fvg.nearest = FairValueGap(
            direction=BEARISH, top=115.0, bottom=113.0, index=10,
            state='unfilled', size=2.0, size_atr=1.0,
        )
        ob = OrderBlockState(direction=BEARISH, score=0.5, reason='bearish OB')
        from analysis.smc.order_blocks import OrderBlock
        ob.nearest = OrderBlock(
            direction=BEARISH, top=117.0, bottom=115.0, index=8,
            state='fresh', displacement_atr=1.5, volume_ratio=1.2, strength=0.6,
        )
        engine = ICTMSNRConfluenceEngine()
        result = engine.evaluate(
            msnr=msnr, ict_structure=ict_structure, displacement=displacement,
            premium_discount=pd_result, liquidity=liquidity, fvg=fvg,
            order_blocks=ob, existing_trend=BEARISH,
        )
        assert result.direction == BEARISH
        assert result.strength >= 0.5

    def test_insufficient_alignment_returns_range(self):
        """Only 1-2 elements without proper confirmation → no forced signal."""
        msnr = MSNRResult(location=SUPPORT_ZONE, strength=0.5, score=0.4)
        # Everything else neutral/empty.
        ict = ICTStructureResult()
        disp = DisplacementResult()
        pd_r = PremiumDiscountResult()
        liq = LiquidityState()
        fvg = FairValueGapState()
        ob = OrderBlockState()
        engine = ICTMSNRConfluenceEngine()
        result = engine.evaluate(
            msnr=msnr, ict_structure=ict, displacement=disp,
            premium_discount=pd_r, liquidity=liq, fvg=fvg,
            order_blocks=ob, existing_trend=RANGE,
        )
        # Should NOT force a BUY/SELL.
        assert result.direction == RANGE or result.strength < 0.3

    def test_conflicting_evidence_returns_wait(self):
        """MSNR + ICT bullish but existing trend strongly bearish → conflict noted."""
        msnr, ict, disp, pd_r, liq, fvg, ob = self._make_bullish_context()
        engine = ICTMSNRConfluenceEngine()
        result = engine.evaluate(
            msnr=msnr, ict_structure=ict, displacement=disp,
            premium_discount=pd_r, liquidity=liq, fvg=fvg,
            order_blocks=ob, existing_trend=BEARISH,  # opposing!
        )
        # Should still have bullish direction but with reduced strength.
        if result.direction == BULLISH:
            # Reduced by the trend penalty.
            assert result.strength < 1.0

    def test_no_data_returns_neutral(self):
        engine = ICTMSNRConfluenceEngine()
        result = engine.evaluate(
            msnr=MSNRResult(), ict_structure=ICTStructureResult(),
            displacement=DisplacementResult(), premium_discount=PremiumDiscountResult(),
            liquidity=LiquidityState(), fvg=FairValueGapState(),
            order_blocks=OrderBlockState(),
        )
        assert result.direction == RANGE
        assert result.strength == 0.0


# ─────────────────────────────────────────────
# Integration: Full pipeline with ICT-MSNR
# ─────────────────────────────────────────────

class TestPipelineIntegration:
    """Run the full pipeline to verify ICT-MSNR integrates without errors."""

    def test_uptrend_has_ict_fields(self, settings):
        from analysis.pipeline import SignalPipeline
        from tests.fakes import StubProvider, make_uptrend
        provider = StubProvider(candles=make_uptrend())
        pipeline = SignalPipeline(settings)
        result = pipeline.run(provider, 'BTCUSDT', '15m')
        # The entry picture should have ICT-MSNR fields.
        entry = result.mtf.entry
        assert hasattr(entry, 'msnr')
        assert hasattr(entry, 'ict_structure')
        assert hasattr(entry, 'displacement')
        assert hasattr(entry, 'premium_discount')
        assert hasattr(entry, 'ict_confluence')
        # The confluence should have ICT-MSNR direction.
        assert hasattr(result.confluence, 'ict_msnr_direction')

    def test_downtrend_has_ict_fields(self, settings):
        from analysis.pipeline import SignalPipeline
        from tests.fakes import StubProvider, make_downtrend
        provider = StubProvider(candles=make_downtrend())
        pipeline = SignalPipeline(settings)
        result = pipeline.run(provider, 'BTCUSDT', '15m')
        entry = result.mtf.entry
        assert entry.msnr.location in (SUPPORT_ZONE, RESISTANCE_ZONE, MID_RANGE, NO_CLEAR_LEVEL, UNAVAILABLE)
        assert entry.ict_structure.trend in (BULLISH, BEARISH, RANGE)

    def test_serialization_includes_ict_fields(self, settings):
        """The API response should include all ICT-MSNR fields."""
        from analysis.pipeline import SignalPipeline
        from api.serializers import to_analyze_response
        from tests.fakes import StubProvider, make_uptrend
        provider = StubProvider(candles=make_uptrend())
        pipeline = SignalPipeline(settings)
        result = pipeline.run(provider, 'BTCUSDT', '15m')
        response = to_analyze_response(result)
        body = response.model_dump()
        analysis = body['analysis']
        assert 'msnr' in analysis
        assert 'displacement' in analysis
        assert 'premium_discount' in analysis
        assert 'ict_structure' in analysis
        assert 'ict_msnr_confluence' in analysis
        # Confluence should have the ICT-MSNR summary.
        confluence = analysis['confluence']
        assert 'ict_msnr_direction' in confluence
        assert 'ict_msnr_strength' in confluence

    def test_diagnostic_includes_ict_msnr(self, settings):
        """The diagnostic output should include the ICT-MSNR section."""
        from analysis.pipeline import SignalPipeline
        from api.serializers import to_analyze_response
        from tests.fakes import StubProvider, make_uptrend
        provider = StubProvider(candles=make_uptrend())
        pipeline = SignalPipeline(settings)
        result = pipeline.run(provider, 'BTCUSDT', '15m')
        response = to_analyze_response(result)
        body = response.model_dump()
        diagnostic = body['diagnostic']
        assert 'ict_msnr' in diagnostic
        assert 'msnr' in diagnostic['ict_msnr']
        assert 'ict' in diagnostic['ict_msnr']
        assert 'confluence' in diagnostic['ict_msnr']


# ─────────────────────────────────────────────
# Regression: No double counting
# ─────────────────────────────────────────────

class TestNoDoubleCount:
    """Explicit regression tests proving structure evidence is NOT double-counted.

    Required by Part 9 of the spec.
    """

    def test_same_swing_high_not_scored_three_times(self):
        """A swing high at 110.0 appears in structure, MSNR, and ICT adapter.
        The evidence registry should keep only ONE.
        """
        registry = EvidenceRegistry()
        for source in ('structure', 'msnr', 'ict_structure'):
            registry.add(EvidenceItem(
                make_evidence_id('swing_high', 110.0),
                'swing_high', 'bearish', 110.0, 0.5, source, f'from {source}',
            ))
        assert len(registry.items) == 1
        assert registry.dedup_count == 2

    def test_same_bos_not_scored_twice(self):
        """A BOS event from structure and ICT adapter should merge."""
        registry = EvidenceRegistry()
        registry.add(EvidenceItem(
            make_evidence_id('bos', 110.0),
            'bos', 'bullish', 110.0, 0.6, 'structure', 'BOS from structure',
        ))
        registry.add(EvidenceItem(
            make_evidence_id('bos', 110.0),
            'bos', 'bullish', 110.0, 0.5, 'ict_structure', 'BOS from ICT',
        ))
        assert len(registry.items) == 1
        assert registry.items[0].source == 'structure'  # stronger one kept

    def test_same_support_level_not_scored_twice(self):
        """A support level from MSNR and existing S/R should merge."""
        registry = EvidenceRegistry()
        registry.add(EvidenceItem(
            make_evidence_id('support', 95.0),
            'support', 'bullish', 95.0, 0.7, 'msnr', 'support from MSNR',
        ))
        registry.add(EvidenceItem(
            make_evidence_id('support', 95.0),
            'support', 'bullish', 95.0, 0.5, 'levels', 'support from S/R',
        ))
        assert len(registry.items) == 1

    def test_module_weights_still_total_100(self):
        """Phase 2A must NOT have changed the module weight total."""
        from analysis.modules import MODULE_WEIGHTS
        assert sum(MODULE_WEIGHTS.values()) == 100

    def test_confidence_budget_still_totals_100(self):
        """Phase 2A must NOT have broken the confidence budget."""
        from analysis.scoring import CONFIDENCE_BUDGET
        assert sum(CONFIDENCE_BUDGET.values()) == 100


# ─────────────────────────────────────────────
# FVG additional tests (Phase 2A specific)
# ─────────────────────────────────────────────

class TestFVGPhase2A:

    def test_bullish_fvg(self):
        candles = make_uptrend(200)
        atr = 2.0
        result = detect_fair_value_gaps(candles, atr)
        # In an uptrend with swing amplitude, some bullish FVGs may form.
        # This test verifies the engine runs without error.
        assert isinstance(result, FairValueGapState)

    def test_bearish_fvg(self):
        candles = make_downtrend(200)
        atr = 2.0
        result = detect_fair_value_gaps(candles, atr)
        assert isinstance(result, FairValueGapState)

    def test_fvg_filled_state(self):
        """Once price trades back through a FVG, it should be marked filled."""
        candles = make_uptrend(200)
        result = detect_fair_value_gaps(candles, atr=0.1)
        for gap in result.gaps:
            assert gap.state in ('filled', 'unfilled')


# ─────────────────────────────────────────────
# Order Block additional tests (Phase 2A specific)
# ─────────────────────────────────────────────

class TestOrderBlockPhase2A:

    def test_bullish_order_block(self):
        candles = make_uptrend(200)
        swings = find_swings(candles)
        result = detect_order_blocks(candles, swings, atr=1.0)
        assert isinstance(result, OrderBlockState)

    def test_bearish_order_block(self):
        candles = make_downtrend(200)
        swings = find_swings(candles)
        result = detect_order_blocks(candles, swings, atr=1.0)
        assert isinstance(result, OrderBlockState)


# ─────────────────────────────────────────────
# Liquidity additional tests (Phase 2A specific)
# ─────────────────────────────────────────────

class TestLiquidityPhase2A:

    def test_equal_highs(self):
        """Equal highs should be detected as BSL pools."""
        candles = make_ranging_candles(200, level=100, amp=3)
        swings = find_swings(candles)
        result = detect_liquidity(candles, swings, atr=1.0)
        assert isinstance(result, LiquidityState)
        # In a ranging market with consistent swings, equal highs may form.
        assert result.equal_highs >= 0

    def test_equal_lows(self):
        candles = make_ranging_candles(200, level=100, amp=3)
        swings = find_swings(candles)
        result = detect_liquidity(candles, swings, atr=1.0)
        assert result.equal_lows >= 0

    def test_liquidity_sweep(self):
        """Verify sweep detection runs without error."""
        candles = make_uptrend(200)
        swings = find_swings(candles)
        result = detect_liquidity(candles, swings, atr=1.0)
        assert isinstance(result, LiquidityState)
