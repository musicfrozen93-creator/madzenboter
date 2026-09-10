"""Strategy registry — the canonical answer to "what does this strategy analyse?".

THE SINGLE SOURCE OF TRUTH. The dashboard, the offline fallback mirror and any
API client all read strategy definitions from here (via GET /api/indicators);
none of them defines its own mapping. A client sends a ``strategy_id`` and the
backend resolves it — a client can never describe a strategy's contents itself.

A strategy is a NAMED SELECTION over the existing module registry
(``analysis.modules``). It introduces no new indicator, no new calculation and
no new scoring path:

    weighted modules      cast votes and carry Quality Score weight
    contextual toggles    gate which ICT/MSNR evidence is read (weight 0)
    evidence carriers     the weighted modules that score shared evidence once
    confluence            the relationship read across those layers

Selecting a strategy switches modules on and off. It never changes how any of
them is scored — MODULE_WEIGHTS, CONFIDENCE_BUDGET and every decision threshold
are untouched by this file.

VERSIONING
    Each strategy carries an id ending in a version (``ICT_MSNR_V1``). Changing
    what a strategy contains means publishing a NEW id, never editing an old one
    in place — a saved analysis records the id that produced it, so historical
    results are never silently reinterpreted under a newer definition.

NO PERFORMANCE CLAIMS
    Descriptions state what a strategy ANALYSES. They never claim profitability,
    win rate or superiority; nothing here has been backtested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

from analysis.modules import (
    ADX,
    ATR,
    CONFIRMATION_MODULES,
    FIBONACCI,
    FVG,
    ICT_BOS,
    ICT_CONFLUENCE,
    ICT_DISPLACEMENT,
    ICT_FVG,
    ICT_LIQUIDITY,
    ICT_LIQUIDITY_SWEEPS,
    ICT_MODULES,
    ICT_MSNR_CARRIERS,
    ICT_MSS,
    ICT_ORDER_BLOCKS,
    ICT_PREMIUM_DISCOUNT,
    LIQUIDITY,
    MACD,
    MSNR_KEY_SR,
    MSNR_LOCATION,
    MSNR_MODULES,
    MSNR_PDHL,
    MSNR_PWHL,
    MSNR_RESISTANCE_ZONES,
    MSNR_SUPPORT_ZONES,
    MSNR_SWING_LEVELS,
    OPTIONAL_UI_KEYS,
    ORDER_BLOCK,
    PATTERN,
    REQUIRED_MODULES,
    RSI,
    SUPPORT_RESISTANCE,
    VOLUME,
    VWAP,
)

#: Sentinel id for "the user configured modules by hand".
CUSTOM_STRATEGY_ID = 'CUSTOM'


class StrategyStatus:
    """How far a strategy has been validated. The only permitted values.

    PRODUCTION    Designed and audited for live use. May produce a tradeable
                  BUY/SELL and may enter the Live Signals feed.
    EXPERIMENTAL  Available for research and preview. The engine will run the
                  full analysis, but the decision layer downgrades the verdict
                  to WAIT — an unvalidated strategy must never be presented to a
                  paying user as a tradeable signal.
    CUSTOM        The user's own module selection — a research and
                  configuration mode, not a validated strategy. Runs the full
                  analysis and reports bias and scores, but the decision layer
                  returns WAIT: nobody has validated the combination the user
                  assembled, so it must not read as a trade recommendation.

    A strategy is PRODUCTION only when its methodology has actually been
    designed and audited. Backtested performance is NOT claimed for any status —
    nothing in this codebase has been backtested.
    """

    PRODUCTION = 'production'
    EXPERIMENTAL = 'experimental'
    CUSTOM = 'custom'

    ALL = frozenset({PRODUCTION, EXPERIMENTAL, CUSTOM})

    #: Statuses whose signals may reach the Live Signals feed.
    LIVE_SIGNAL_ELIGIBLE = frozenset({PRODUCTION})

    #: Statuses whose verdict is forced to WAIT regardless of the scores.
    #:
    #: CUSTOM is here alongside EXPERIMENTAL because a hand-assembled module
    #: selection is a research configuration, not a validated methodology — the
    #: user picked the inputs, nobody validated the combination. Production
    #: policy is that only a strategy someone has actually designed and audited
    #: may tell a paying user to take a trade. ICT + MSNR is currently the only
    #: one, so it is the only status left out of this set.
    NOT_TRADEABLE = frozenset({EXPERIMENTAL, CUSTOM})

    #: The decision-layer reason code each blocked status reports. Distinct
    #: codes so a client can tell "unvalidated strategy" from "your own
    #: configuration" without parsing prose.
    BLOCKED_REASON_CODE = {
        EXPERIMENTAL: 'experimental_strategy',
        CUSTOM: 'custom_strategy',
    }


@dataclass(frozen=True)
class Strategy:
    """One named analysis configuration.

    ``enabled_modules`` names OPTIONAL keys only. ``locked_modules`` are the
    required core modules every strategy always runs — they are not selectable
    and never appear as a checkbox.
    """

    strategy_id: str
    name: str
    description: str
    category: str
    enabled_modules: FrozenSet[str]
    locked_modules: FrozenSet[str] = field(default_factory=lambda: frozenset(REQUIRED_MODULES))
    confirmation_modules: FrozenSet[str] = field(default_factory=frozenset)
    recommended: bool = False
    version: int = 1
    status: str = StrategyStatus.EXPERIMENTAL

    @property
    def is_production(self) -> bool:
        return self.status == StrategyStatus.PRODUCTION

    @property
    def blocks_tradeable(self) -> bool:
        """True when this strategy may never emit a tradeable BUY/SELL."""
        return self.status in StrategyStatus.NOT_TRADEABLE

    @property
    def live_signal_eligible(self) -> bool:
        """True when this strategy's signals may enter the Live Signals feed."""
        return self.status in StrategyStatus.LIVE_SIGNAL_ELIGIBLE

    @property
    def modules(self) -> FrozenSet[str]:
        """Every module this strategy runs — locked core plus its selection."""
        return self.locked_modules | self.enabled_modules

    def as_dict(self) -> dict:
        return {
            'strategy_id': self.strategy_id,
            'name': self.name,
            'description': self.description,
            'category': self.category,
            'enabled_modules': sorted(self.enabled_modules),
            'locked_modules': sorted(self.locked_modules),
            'confirmation_modules': sorted(self.confirmation_modules),
            'recommended': self.recommended,
            'version': self.version,
            'status': self.status,
        }


# ─────────────────────────────────────────────────────────────────────────────
# The strategies
# ─────────────────────────────────────────────────────────────────────────────
# Two rules govern every definition below. Both are enforced by assertions at
# import time, because breaking either produces a strategy that silently does
# less than it claims:
#
#  1. CONFLUENCE COHERENCE — a strategy that switches on ICT or MSNR toggles
#     must also switch on ICT_CONFLUENCE. Those toggles are contextual: without
#     the confluence they feed nothing and change no score.
#
#  2. CARRIER COHERENCE — a strategy that switches on an ICT/MSNR toggle must
#     also switch on the weighted module that carries its evidence into the
#     score. An ICT FVG with no FVG module is evidence nothing can weigh.

_ICT_MSNR = Strategy(
    strategy_id='ICT_MSNR_V1',
    name='ICT + MSNR',
    description='Smart-money, liquidity and market-structure analysis.',
    category='smart_money',
    enabled_modules=frozenset(
        ICT_MODULES + MSNR_MODULES + CONFIRMATION_MODULES + ICT_MSNR_CARRIERS
    ),
    confirmation_modules=frozenset(CONFIRMATION_MODULES),
    recommended=True,
    # The one methodology that has actually been designed and audited here.
    status=StrategyStatus.PRODUCTION,
)

_TREND_FOLLOWING = Strategy(
    strategy_id='TREND_FOLLOWING_V1',
    name='Trend Following',
    description='Trend continuation and directional momentum analysis.',
    category='trend',
    # Directional strength and momentum, with Fibonacci for continuation entries
    # and S/R for the levels a trend has to clear.
    enabled_modules=frozenset({
        ADX, MACD, FIBONACCI, SUPPORT_RESISTANCE,
    } | set(CONFIRMATION_MODULES)),
    confirmation_modules=frozenset(CONFIRMATION_MODULES),
    # Composed over the module registry, not derived from validated trading
    # rules and never backtested — research only until that work is done.
    status=StrategyStatus.EXPERIMENTAL,
)

_BREAKOUT_RETEST = Strategy(
    strategy_id='BREAKOUT_RETEST_V1',
    name='Breakout & Retest',
    description='Breakout confirmation, retest and structural continuation.',
    category='breakout',
    # The levels a breakout clears (MSNR zones, S/R), the displacement that
    # carries it, the liquidity it takes, and the gaps/blocks it retests.
    enabled_modules=frozenset({
        SUPPORT_RESISTANCE, PATTERN, ORDER_BLOCK, FVG, LIQUIDITY,
        ICT_BOS, ICT_MSS, ICT_DISPLACEMENT, ICT_LIQUIDITY,
        ICT_LIQUIDITY_SWEEPS, ICT_FVG, ICT_ORDER_BLOCKS, ICT_CONFLUENCE,
        MSNR_SUPPORT_ZONES, MSNR_RESISTANCE_ZONES, MSNR_PDHL, MSNR_PWHL,
        MSNR_SWING_LEVELS, MSNR_KEY_SR, MSNR_LOCATION,
    } | set(CONFIRMATION_MODULES)),
    confirmation_modules=frozenset(CONFIRMATION_MODULES),
    # Composed over the module registry, not derived from validated trading
    # rules and never backtested — research only until that work is done.
    status=StrategyStatus.EXPERIMENTAL,
)

_MEAN_REVERSION = Strategy(
    strategy_id='MEAN_REVERSION_V1',
    name='Mean Reversion',
    description='Range, overextension and reversion setups.',
    category='mean_reversion',
    # Where price sits relative to its range: RSI extremes, Fibonacci
    # retracement, VWAP as the mean, and the zones price reverts to.
    enabled_modules=frozenset({
        RSI, FIBONACCI, SUPPORT_RESISTANCE,
        ICT_PREMIUM_DISCOUNT, ICT_CONFLUENCE,
        MSNR_SUPPORT_ZONES, MSNR_RESISTANCE_ZONES, MSNR_KEY_SR,
        MSNR_SWING_LEVELS, MSNR_LOCATION,
    } | set(CONFIRMATION_MODULES)),
    confirmation_modules=frozenset(CONFIRMATION_MODULES),
    # Composed over the module registry, not derived from validated trading
    # rules and never backtested — research only until that work is done.
    status=StrategyStatus.EXPERIMENTAL,
)

_MOMENTUM = Strategy(
    strategy_id='MOMENTUM_V1',
    name='Momentum',
    description='Momentum and directional strength analysis.',
    category='momentum',
    enabled_modules=frozenset({
        RSI, MACD, ADX,
    } | set(CONFIRMATION_MODULES)),
    confirmation_modules=frozenset(CONFIRMATION_MODULES),
    # Composed over the module registry, not derived from validated trading
    # rules and never backtested — research only until that work is done.
    status=StrategyStatus.EXPERIMENTAL,
)

#: Ordered as the dashboard renders them — recommended first.
STRATEGY_ORDER: Tuple[Strategy, ...] = (
    _ICT_MSNR,
    _TREND_FOLLOWING,
    _BREAKOUT_RETEST,
    _MEAN_REVERSION,
    _MOMENTUM,
)

STRATEGIES: Dict[str, Strategy] = {s.strategy_id: s for s in STRATEGY_ORDER}

#: What a NEW analysis uses when the user has expressed no preference.
DEFAULT_STRATEGY_ID: str = _ICT_MSNR.strategy_id

#: Presented in the UI as a strategy, but it has no fixed module list — the
#: user's own selection is the definition, so it never appears in STRATEGIES.
CUSTOM_STRATEGY = Strategy(
    strategy_id=CUSTOM_STRATEGY_ID,
    name='Custom',
    description='Manual analysis configuration — research and configuration only.',
    category='custom',
    enabled_modules=frozenset(),
    version=1,
    status=StrategyStatus.CUSTOM,
)


# ── Import-time coherence checks ─────────────────────────────────────────────

def _carrier_requirements(keys: FrozenSet[str]) -> Dict[str, str]:
    """Which weighted carrier each contextual key needs to be scored at all."""
    needs: Dict[str, str] = {}
    if ICT_FVG in keys:
        needs[ICT_FVG] = FVG
    if ICT_ORDER_BLOCKS in keys:
        needs[ICT_ORDER_BLOCKS] = ORDER_BLOCK
    if keys & {ICT_LIQUIDITY, ICT_LIQUIDITY_SWEEPS}:
        needs['ict liquidity'] = LIQUIDITY
    if keys & set(MSNR_MODULES):
        needs['msnr zones'] = SUPPORT_RESISTANCE
    return needs


for _s in STRATEGY_ORDER:
    _unknown = _s.enabled_modules - OPTIONAL_UI_KEYS
    assert not _unknown, f'{_s.strategy_id} names unknown modules: {sorted(_unknown)}'
    assert not (_s.enabled_modules & REQUIRED_MODULES), (
        f'{_s.strategy_id} lists a locked core module as selectable'
    )
    _contextual = _s.enabled_modules & (set(ICT_MODULES) | set(MSNR_MODULES))
    if _contextual - {ICT_CONFLUENCE}:
        assert ICT_CONFLUENCE in _s.enabled_modules, (
            f'{_s.strategy_id} enables ICT/MSNR toggles without ICT_CONFLUENCE — '
            'they would feed nothing and change no score'
        )
    for _key, _carrier in _carrier_requirements(_s.enabled_modules).items():
        assert _carrier in _s.enabled_modules, (
            f'{_s.strategy_id} enables {_key} without its evidence carrier '
            f'{_carrier}; the evidence would have no scoring path'
        )

assert DEFAULT_STRATEGY_ID in STRATEGIES
assert sum(1 for s in STRATEGY_ORDER if s.recommended) == 1, (
    'exactly one strategy may be marked recommended'
)
for _s in list(STRATEGY_ORDER) + [CUSTOM_STRATEGY]:
    assert _s.status in StrategyStatus.ALL, f'{_s.strategy_id}: bad status {_s.status}'
    # Recommending a strategy asserts it is fit for live use. Only a production
    # strategy may carry that claim.
    if _s.recommended:
        assert _s.is_production, f'{_s.strategy_id} is recommended but not production'
assert STRATEGIES[DEFAULT_STRATEGY_ID].is_production, (
    'the default strategy must be production-status'
)
# PRODUCTION is the only status that may emit a tradeable BUY/SELL, and every
# blocked status must name the reason code it reports.
assert StrategyStatus.ALL - StrategyStatus.NOT_TRADEABLE == {StrategyStatus.PRODUCTION}
assert set(StrategyStatus.BLOCKED_REASON_CODE) == StrategyStatus.NOT_TRADEABLE
assert len(set(StrategyStatus.BLOCKED_REASON_CODE.values())) == len(
    StrategyStatus.BLOCKED_REASON_CODE
), 'each blocked status needs a distinct reason code'


# ── Resolution ───────────────────────────────────────────────────────────────

def get_strategy(strategy_id: Optional[str]) -> Optional[Strategy]:
    """Look a strategy up by id. Returns ``None`` for unknown/custom ids."""
    if not strategy_id:
        return None
    return STRATEGIES.get(str(strategy_id).strip().upper())


def strategy_modules(strategy_id: Optional[str]) -> FrozenSet[str]:
    """Resolve a strategy id to the module set the engine should run.

    An unknown id falls back to the default strategy rather than raising, so a
    stale client can never break analysis. Always a superset of the locked core.
    """
    strategy = get_strategy(strategy_id) or STRATEGIES[DEFAULT_STRATEGY_ID]
    return strategy.modules


def identify_strategy(active, declared_id: Optional[str] = None) -> Strategy:
    """Name the strategy a module selection corresponds to.

    A selection matches a strategy only when BOTH its optional keys and its
    locked core are exactly that strategy's. Checking the core matters because
    a caller reaching the engine directly (a scanner, a batch job) can supply a
    set without trend/structure — 30 of the 100 quality points — and it must not
    be graded as the production methodology on the strength of its optional
    keys alone.

    ``declared_id`` is what the CALLER said it was running. An explicit
    ``CUSTOM`` declaration is honoured even when the modules happen to coincide
    with a registered strategy: the user assembled that set by hand, the UI
    labels it Custom, and a research configuration must not silently become a
    tradeable production signal because it happened to match. Declaring a
    strategy can only ever make the result MORE restrictive, never less — an
    unrecognised or absent declaration falls through to the module comparison.
    """
    if declared_id and str(declared_id).strip().upper() == CUSTOM_STRATEGY_ID:
        return CUSTOM_STRATEGY
    active = frozenset(active)
    optional = active & OPTIONAL_UI_KEYS
    for strategy in STRATEGY_ORDER:
        if optional == strategy.enabled_modules and strategy.locked_modules <= active:
            return strategy
    return CUSTOM_STRATEGY


def strategy_status(active, declared_id: Optional[str] = None) -> str:
    """The validation status of whatever module set actually ran.

    Derived from the modules the ENGINE used, never from what a client claimed,
    so the restriction on experimental strategies cannot be bypassed by sending
    a hand-built module list that happens to match one.
    """
    return identify_strategy(active, declared_id).status


def blocked_reason_code(active, declared_id: Optional[str] = None) -> str:
    """The decision-layer reason code for a module set that may not trade.

    Only meaningful when :func:`blocks_tradeable` is True.
    """
    status = identify_strategy(active, declared_id).status
    return StrategyStatus.BLOCKED_REASON_CODE.get(status, 'experimental_strategy')


def blocks_tradeable(active, declared_id: Optional[str] = None) -> bool:
    """True when the module set that ran may not emit a tradeable BUY/SELL."""
    return identify_strategy(active, declared_id).blocks_tradeable


def live_signal_eligible(active, declared_id: Optional[str] = None) -> bool:
    """True when a signal from this module set may enter the Live Signals feed."""
    return identify_strategy(active, declared_id).live_signal_eligible


def describe_strategies() -> List[dict]:
    """The strategy catalogue, for the configuration UI.

    The dashboard renders this list; it never hardcodes strategies of its own,
    so a strategy added here appears in the UI without a frontend change.
    """
    return [s.as_dict() for s in STRATEGY_ORDER] + [CUSTOM_STRATEGY.as_dict()]
