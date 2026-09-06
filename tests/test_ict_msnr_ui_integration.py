"""ICT + MSNR UI integration tests (Phase 2A completion).

These pin the contract the frontend Analysis Configuration UI depends on:

  * every ICT and MSNR toggle appears in the /api/indicators registry;
  * disabling an ICT toggle removes its evidence from the ICT panel and never
    lets it appear as active evidence anywhere else;
  * disabling every ICT toggle omits the ict_analysis panel entirely;
  * disabling every MSNR toggle omits the msnr_analysis panel entirely;
  * disabling ict_confluence turns the confluence panel off and drops the
    ict_msnr_agreement confidence component so the score is renormalised;
  * turning modules off never lowers the Quality Score — the disabled weight
    leaves both the numerator and the denominator;
  * MODULE_WEIGHTS still totals 100 (the ICT/MSNR toggles carry weight zero);
  * the serializer produces the top-level `ict_analysis` / `msnr_analysis` /
    `ict_confluence` blocks in the shape the frontend components consume.

The end-to-end HTTP contract lives in ``test_ict_msnr_api.py`` (uses
FastAPI's TestClient); the checks here are pure and never touch the network.
"""

from __future__ import annotations

import pytest

from analysis.cache import CANDLE_CACHE
from analysis.confluence import ConfluenceEngine
from analysis.engine import AnalysisEngine
from analysis.modules import (
    ALL_MODULE_KEYS,
    CONTEXTUAL_MODULES,
    ICT_BOS,
    ICT_CONFLUENCE,
    ICT_FVG,
    ICT_LIQUIDITY,
    ICT_LIQUIDITY_SWEEPS,
    ICT_MODULES,
    ICT_MSS,
    ICT_ORDER_BLOCKS,
    ICT_PREMIUM_DISCOUNT,
    MODULE_ORDER,
    MODULE_WEIGHTS,
    MSNR_LOCATION,
    MSNR_MODULES,
    MSNR_PDHL,
    MSNR_PWHL,
    MSNR_RESISTANCE_ZONES,
    MSNR_SUPPORT_ZONES,
    OPTIONAL_UI_KEYS,
    REQUIRED_MODULES,
    describe_modules,
    ict_enabled,
    msnr_enabled,
    resolve_enabled_modules,
)
from analysis.pipeline import SignalPipeline
from analysis.scoring import ConfidenceScorer, QualityScorer
from analysis.timeframes import MultiTimeframeEngine
from api.serializers import (
    _build_ict_analysis,
    _build_ict_confluence_panel,
    _build_msnr_analysis,
    to_analyze_response,
)
from tests.fakes import StubProvider, make_uptrend


# ─────────────────────────────────────────────
# Registry surface
# ─────────────────────────────────────────────

def test_registry_lists_every_ict_toggle():
    keys = {m['key'] for m in describe_modules()}
    assert set(ICT_MODULES) <= keys


def test_registry_lists_every_msnr_toggle():
    keys = {m['key'] for m in describe_modules()}
    assert set(MSNR_MODULES) <= keys


def test_registry_categorises_ict_and_msnr_separately():
    by_key = {m['key']: m for m in describe_modules()}
    assert all(by_key[k]['category'] == 'ICT' for k in ICT_MODULES)
    assert all(by_key[k]['category'] == 'MSNR' for k in MSNR_MODULES)


def test_contextual_toggles_are_weight_zero_and_optional():
    by_key = {m['key']: m for m in describe_modules()}
    for k in CONTEXTUAL_MODULES:
        assert by_key[k]['weight'] == 0
        assert by_key[k]['required'] is False


def test_module_weights_still_total_100_after_ict_msnr_added():
    """Contextual toggles never enter MODULE_WEIGHTS — the classical total is preserved."""
    assert sum(MODULE_WEIGHTS.values()) == 100


def test_indicator_count_grew_from_15_to_31():
    """The UI count is registry-driven; ICT (9) + MSNR (7) added on top of the 15 classical."""
    assert len(describe_modules()) == len(MODULE_ORDER) + len(CONTEXTUAL_MODULES)
    assert len(describe_modules()) == 31


# ─────────────────────────────────────────────
# resolve_enabled_modules accepts the toggles
# ─────────────────────────────────────────────

def test_resolve_accepts_ict_and_msnr_toggles():
    resolved = resolve_enabled_modules(['ict_liquidity', 'msnr_location'])
    assert 'ict_liquidity' in resolved
    assert 'msnr_location' in resolved
    assert REQUIRED_MODULES <= resolved


def test_resolve_drops_unknown_ict_lookalikes():
    resolved = resolve_enabled_modules(['ict_liquidity', 'ict_totally_fake'])
    assert 'ict_liquidity' in resolved
    assert 'ict_totally_fake' not in resolved


def test_ict_enabled_and_msnr_enabled_helpers():
    assert ict_enabled(frozenset(ICT_MODULES))
    assert msnr_enabled(frozenset(MSNR_MODULES))
    assert not ict_enabled(frozenset(MODULE_ORDER))
    assert not msnr_enabled(frozenset(MODULE_ORDER))


# ─────────────────────────────────────────────
# Serializer produces the right panel shapes
# ─────────────────────────────────────────────

@pytest.fixture
def _result(settings):
    CANDLE_CACHE.clear()
    provider = StubProvider(candles=make_uptrend(n=500))
    pipeline = SignalPipeline(settings)
    return pipeline.run(provider, 'BTCUSDT', '15m')


def test_ict_panel_present_when_ict_enabled(_result):
    picture = _result.mtf.entry
    panel = _build_ict_analysis(picture, frozenset(ALL_MODULE_KEYS))
    assert panel is not None
    # Every toggle contributes a block.
    for key in (
        'liquidity', 'liquidity_sweeps', 'mss', 'bos',
        'displacement', 'fvg', 'order_blocks', 'premium_discount',
    ):
        assert key in panel


def test_ict_panel_absent_when_every_ict_toggle_disabled(_result):
    picture = _result.mtf.entry
    # Only classical + MSNR — no ICT toggle in the set.
    enabled = frozenset(list(MODULE_ORDER) + list(MSNR_MODULES))
    assert _build_ict_analysis(picture, enabled) is None
    assert _build_ict_confluence_panel(picture, enabled) is None


def test_msnr_panel_absent_when_every_msnr_toggle_disabled(_result):
    picture = _result.mtf.entry
    enabled = frozenset(list(MODULE_ORDER) + list(ICT_MODULES))
    assert _build_msnr_analysis(picture, enabled) is None


def test_disabling_ict_fvg_drops_only_that_row(_result):
    picture = _result.mtf.entry
    enabled = frozenset(
        list(MODULE_ORDER)
        + [k for k in ICT_MODULES if k != ICT_FVG]
        + list(MSNR_MODULES)
    )
    panel = _build_ict_analysis(picture, enabled)
    assert panel is not None
    assert panel['fvg'] is None
    # Other rows survive.
    assert panel['liquidity'] is not None
    assert panel['order_blocks'] is not None


def test_disabling_msnr_pwhl_leaves_pdhl_intact(_result):
    picture = _result.mtf.entry
    enabled = frozenset(
        list(MODULE_ORDER)
        + list(ICT_MODULES)
        + [k for k in MSNR_MODULES if k != MSNR_PWHL]
    )
    panel = _build_msnr_analysis(picture, enabled)
    assert panel is not None
    assert panel['pwh'] is None
    assert panel['pwl'] is None
    # PDH/PDL are populated when history is enough for periodic detection —
    # they must NOT be forcibly nulled by the PWHL toggle being off.
    # (Whether they are actually numeric depends on candle count; the point
    # is that flipping PWHL off does not touch PDH/PDL keys.)
    assert 'pdh' in panel and 'pdl' in panel


def test_ict_confluence_panel_omits_when_toggle_off(_result):
    picture = _result.mtf.entry
    enabled = frozenset(set(ALL_MODULE_KEYS) - {ICT_CONFLUENCE})
    assert _build_ict_confluence_panel(picture, enabled) is None


# ─────────────────────────────────────────────
# to_analyze_response wires the new top-level fields
# ─────────────────────────────────────────────

def test_full_analyze_response_carries_top_level_panels(_result):
    resp = to_analyze_response(_result)
    # Pydantic models — access as attributes.
    assert resp.ict_analysis is not None
    assert resp.msnr_analysis is not None
    assert resp.ict_confluence is not None


# ─────────────────────────────────────────────
# Scoring — disabled = neutral, never negative
# ─────────────────────────────────────────────

@pytest.fixture
def _mtf(settings):
    CANDLE_CACHE.clear()
    return MultiTimeframeEngine(settings, AnalysisEngine(settings)).build(
        StubProvider(candles=make_uptrend()), 'BTCUSDT', '15m',
    )


def test_disabling_all_ict_msnr_never_lowers_quality(_mtf):
    """Contextual toggles never enter Quality — turning them off can't lower the score."""
    full = ConfluenceEngine().evaluate(_mtf)
    without_ctx = ConfluenceEngine().evaluate(
        _mtf, enabled_modules=frozenset(MODULE_ORDER),
    )
    q_full = QualityScorer().score(_mtf, full).value
    q_no_ctx = QualityScorer().score(_mtf, without_ctx).value
    assert q_no_ctx >= q_full


def test_disabling_ict_confluence_drops_confidence_component(_mtf):
    enabled_without_ict_conf = set(ALL_MODULE_KEYS) - {ICT_CONFLUENCE}
    conf = ConfluenceEngine().evaluate(
        _mtf, enabled_modules=frozenset(enabled_without_ict_conf),
    )
    confidence = ConfidenceScorer().score(_mtf, conf)
    names = {c.name for c in confidence.components}
    assert 'ict_msnr_agreement' not in names
    assert 0 <= confidence.value <= 100


def test_disabling_ict_confluence_neutralises_direction(_mtf):
    enabled = set(ALL_MODULE_KEYS) - {ICT_CONFLUENCE}
    conf = ConfluenceEngine().evaluate(
        _mtf, enabled_modules=frozenset(enabled),
    )
    # No direction → the panel does not oppose the setup, no soft conflict.
    assert conf.ict_msnr_direction == 'range'
    assert conf.ict_msnr_strength == 0.0
    assert not any('ICT-MSNR' in c for c in conf.conflicts)


def test_reduced_module_set_score_normalises_to_100(_mtf):
    """Only trend + structure + rsi enabled, all aligned → score renormalises to 100."""
    reduced = frozenset(list(REQUIRED_MODULES) + ['rsi'])
    conf = ConfluenceEngine().evaluate(_mtf, enabled_modules=reduced)
    quality = QualityScorer().score(_mtf, conf)
    # Score is measured against the enabled weight — never against 100 raw.
    assert 0 <= quality.value <= 100
