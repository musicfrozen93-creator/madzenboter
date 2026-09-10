"""Controlled A–E comparisons: what does enabling ICT + MSNR actually change?

The audit question is not "is the preset wired up" but "does it MOVE anything".
These tests pin the measured answer over a fixed market snapshot so the influence
can never silently regress to zero — and so the size of that influence is stated
in the codebase rather than assumed.

    A  core only                    trend + structure
    B  core + ICT                   + the nine ICT toggles
    C  core + MSNR                  + the seven MSNR toggles
    D  core + ICT + MSNR            both contextual layers
    E  ICT + MSNR + confirmations   the shipped default preset

WHAT THE MEASUREMENTS SHOW (see test_ict_msnr_moves_confidence_but_not_quality):
the contextual layer reaches the final score through exactly ONE channel — the
`ict_msnr_agreement` confidence component. It contributes nothing to quality by
design, because every unit of quality weight belongs to a weighted carrier
module and giving the same gap/block/pool a second home would double-count it.

These tests therefore assert two things at once:
  * the toggles are REAL — turning one off changes the confluence read;
  * the toggles never add a second scoring path for evidence a carrier module
    already scored.
"""

import pytest

from analysis.cache import CANDLE_CACHE
from analysis.confluence import ConfluenceEngine
from analysis.engine import AnalysisEngine
from analysis.modules import (
    ICT_CONFLUENCE,
    ICT_DISPLACEMENT,
    ICT_FVG,
    ICT_LIQUIDITY,
    ICT_LIQUIDITY_SWEEPS,
    ICT_MODULES,
    ICT_ORDER_BLOCKS,
    ICT_PREMIUM_DISCOUNT,
    MODULE_ORDER,
    MODULE_WEIGHTS,
    MSNR_LOCATION,
    MSNR_MODULES,
    REQUIRED_MODULES,
    preset_modules,
)
from analysis.scoring import ConfidenceScorer, QualityScorer
from analysis.structure import RANGE
from analysis.timeframes import MultiTimeframeEngine
from tests.fakes import StubProvider, make_downtrend, make_uptrend

CORE = frozenset(REQUIRED_MODULES)
PRESET = preset_modules('ict_msnr')


class Outcome:
    """One controlled run: what the engine produced for one configuration."""

    def __init__(self, quality, confidence_score, confluence, ict):
        self.quality = quality
        self.confidence = confidence_score.value
        self.confidence_components = {c.name for c in confidence_score.components}
        self.confluence = confluence
        self.ict = ict

    @property
    def elements(self):
        return (
            len(self.ict.bullish_elements)
            + len(self.ict.neutral_elements)
            + len(self.ict.bearish_elements)
        )

    @property
    def enabled_weight(self):
        return sum(
            MODULE_WEIGHTS[m] for m in MODULE_ORDER
            if m in self.confluence.enabled_modules
        )


@pytest.fixture
def analyse(settings):
    """Run the real engine over ONE fixed snapshot with a given module set."""
    def _run(active, candles=None, symbol='BTCUSDT'):
        CANDLE_CACHE.clear()
        provider = StubProvider(candles=candles if candles is not None else make_uptrend(n=500))
        mtf = MultiTimeframeEngine(settings, AnalysisEngine(settings)).build(
            provider, symbol, '15m', enabled_modules=frozenset(active),
        )
        confluence = ConfluenceEngine().evaluate(mtf, enabled_modules=frozenset(active))
        return Outcome(
            QualityScorer().score(mtf, confluence).value,
            ConfidenceScorer().score(mtf, confluence),
            confluence,
            mtf.entry.ict_confluence,
        )
    return _run


# ─────────────────────────────────────────────
# The five controlled cases
# ─────────────────────────────────────────────

def test_case_a_core_only_scores_on_trend_and_structure_alone(analyse):
    a = analyse(CORE)
    assert a.enabled_weight == 30           # trend 15 + structure 15
    assert a.confluence.ict_msnr_direction == RANGE
    assert a.confluence.ict_msnr_strength == 0.0


@pytest.mark.parametrize('label,active', [
    ('B core+ICT', CORE | frozenset(ICT_MODULES)),
    ('C core+MSNR', CORE | frozenset(MSNR_MODULES)),
    ('D core+ICT+MSNR', CORE | frozenset(ICT_MODULES) | frozenset(MSNR_MODULES)),
])
def test_contextual_layers_add_no_quality_weight(analyse, label, active):
    """B, C and D add 16 toggles between them and not one point of weight.

    This is the deliberate anti-double-count contract: the gap/block/pool those
    toggles describe is scored by its weighted carrier, once.
    """
    core = analyse(CORE)
    case = analyse(active)
    assert case.enabled_weight == core.enabled_weight == 30, label
    assert case.quality == core.quality, label


def test_case_e_the_shipped_preset_scores_on_the_carriers(analyse):
    """E is where the strategy actually earns its score — via the carriers."""
    core = analyse(CORE)
    e = analyse(PRESET)
    assert e.enabled_weight == 71
    assert e.enabled_weight > core.enabled_weight
    # More modules scored means the quality reading is a real composite, not
    # trend/structure alone at full marks.
    assert e.quality != core.quality


def test_ict_msnr_moves_confidence_but_not_quality(analyse):
    """The measured influence of the contextual layer, pinned.

    Enabling ICT changes CONFIDENCE (the confluence agreement component) and
    leaves QUALITY untouched. If a future change gives the toggles quality
    weight, that is a double-count and this test must fail.
    """
    core = analyse(CORE)
    with_ict = analyse(CORE | frozenset(ICT_MODULES))
    assert with_ict.quality == core.quality
    assert with_ict.confidence != core.confidence


def test_the_confluence_component_is_the_only_channel(analyse):
    """Drop the confluence switch and the contextual layer stops reaching the score."""
    full = analyse(PRESET)
    no_switch = analyse(PRESET - {ICT_CONFLUENCE})
    no_toggles = analyse(
        frozenset(PRESET) - frozenset(ICT_MODULES) - frozenset(MSNR_MODULES)
    )
    # Same quality either way — the toggles never carried quality weight.
    assert full.quality == no_switch.quality == no_toggles.quality
    # And with the switch off, the 16 toggles are indistinguishable from absent.
    assert no_switch.confidence == no_toggles.confidence
    # The component itself is dropped from the breakdown, not merely zeroed.
    assert 'ict_msnr_agreement' in full.confidence_components
    assert 'ict_msnr_agreement' not in no_switch.confidence_components
    assert 'ict_msnr_agreement' not in no_toggles.confidence_components


# ─────────────────────────────────────────────
# The toggles are REAL, not decorative
# ─────────────────────────────────────────────

@pytest.mark.parametrize('toggle', [
    ICT_FVG,
    ICT_ORDER_BLOCKS,
    ICT_DISPLACEMENT,
    ICT_PREMIUM_DISCOUNT,
    MSNR_LOCATION,
])
def test_switching_one_toggle_off_changes_the_confluence_read(analyse, toggle):
    """Every element toggle must remove its element from the relationship read.

    Before the audit these toggles were decorative: the confluence evaluated all
    seven elements no matter what the user selected, so 15 of the 16 contextual
    checkboxes changed nothing at all.
    """
    full = analyse(PRESET)
    without = analyse(PRESET - {toggle})
    assert without.elements == full.elements - 1, (
        f'{toggle} did not remove its element from the confluence'
    )


def test_switching_liquidity_off_changes_the_confluence_strength(analyse):
    """A removed element renormalises the pattern, so the strength moves."""
    full = analyse(PRESET, candles=make_downtrend(n=500), symbol='ETHUSDT')
    without = analyse(
        PRESET - {ICT_LIQUIDITY, ICT_LIQUIDITY_SWEEPS},
        candles=make_downtrend(n=500), symbol='ETHUSDT',
    )
    assert without.confluence.ict_msnr_strength != full.confluence.ict_msnr_strength


def test_the_confluence_switch_alone_reads_nothing(analyse):
    """The master switch with no element toggles has no evidence to work with."""
    only_switch = analyse(CORE | {ICT_CONFLUENCE})
    assert only_switch.confluence.ict_msnr_direction == RANGE
    assert only_switch.confluence.ict_msnr_strength == 0.0
    assert only_switch.confluence.ict_msnr_explanation == 'ICT confluence disabled'


def test_a_disabled_element_is_excluded_never_counted_against(analyse):
    """Removing an element must not be read as evidence opposing the setup."""
    full = analyse(PRESET, candles=make_downtrend(n=500), symbol='ETHUSDT')
    without = analyse(
        PRESET - {ICT_DISPLACEMENT}, candles=make_downtrend(n=500), symbol='ETHUSDT',
    )
    # The direction may not flip merely because one supporting element was
    # excluded from BOTH the score and the denominator.
    if full.confluence.ict_msnr_direction != RANGE:
        assert without.confluence.ict_msnr_direction in (
            full.confluence.ict_msnr_direction, RANGE
        )


# ─────────────────────────────────────────────
# No double-counting across the five shared kinds
# ─────────────────────────────────────────────

@pytest.mark.parametrize('ict_key,carrier', [
    (ICT_FVG, 'fvg'),
    (ICT_ORDER_BLOCKS, 'order_block'),
    (ICT_LIQUIDITY, 'liquidity'),
    (MSNR_LOCATION, 'support_resistance'),
])
def test_shared_evidence_has_exactly_one_scoring_path(analyse, ict_key, carrier):
    """The contextual view of an event adds no weight; the carrier scores it once."""
    full = analyse(PRESET)
    without_ict = analyse(PRESET - {ict_key})
    assert full.enabled_weight == without_ict.enabled_weight
    assert MODULE_WEIGHTS.get(ict_key, 0) == 0
    assert MODULE_WEIGHTS[carrier] > 0
    # One vote for the pair, from the carrier.
    votes = [v for v in full.confluence.votes if v.module in (ict_key, carrier)]
    assert len(votes) == 1 and votes[0].module == carrier


def test_market_structure_is_scored_once_across_core_and_ict(analyse):
    """ICT's BOS/MSS reading reinterprets the core STRUCTURE module's output.

    It must not appear as a second structural vote.
    """
    full = analyse(PRESET)
    structure_votes = [v for v in full.confluence.votes if 'structure' in v.module]
    assert len(structure_votes) == 1
    assert structure_votes[0].module == 'structure'


def test_the_full_preset_scores_each_weighted_module_exactly_once(analyse):
    full = analyse(PRESET)
    quality_components = [v.module for v in full.confluence.votes]
    assert len(quality_components) == len(set(quality_components))
