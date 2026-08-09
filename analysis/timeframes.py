"""Multi-Timeframe Engine — internal higher-timeframe confirmation.

The user selects ONE timeframe. Internally the engine analyses three:

    role         purpose                          15m      1h      4h
    ─────────────────────────────────────────────────────────────────
    trend        overall market direction         4h       1d      1w
    structure    structure confirmation           1h       4h      1d
    entry        entry confirmation (the user's)  15m      1h      4h

Only the selected timeframe is ever reported back to the user; the higher
timeframes exist to confirm or veto, and stay invisible.

Degradation is graceful. If a venue does not offer a rung, or a pair lacks the
history to compute indicators on it, that rung is dropped and the analysis
continues with the remaining ones. Only the ENTRY rung is mandatory — without it
there is nothing to analyse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from analysis.cache import RequestScope
from analysis.engine import AnalysisEngine, TechnicalPicture
from analysis.structure import BEARISH, BULLISH, RANGE
from providers.base import MarketDataProvider, ProviderError, timeframe_seconds

logger = logging.getLogger(__name__)

TREND = 'trend'
STRUCTURE = 'structure'
ENTRY = 'entry'

# (trend, structure) partners for each selectable entry timeframe.
# The ratios follow the conventional 4x–6x step between timeframes.
TIMEFRAME_LADDER: Dict[str, Tuple[str, str]] = {
    '1m':  ('1h', '15m'),
    '3m':  ('2h', '30m'),
    '5m':  ('4h', '1h'),
    '15m': ('4h', '1h'),
    '30m': ('1d', '4h'),
    '1h':  ('1d', '4h'),
    '2h':  ('1w', '1d'),
    '4h':  ('1w', '1d'),
    '6h':  ('1w', '1d'),
    '8h':  ('1w', '1d'),
    '12h': ('1w', '3d'),
    '1d':  ('1w', '3d'),
    '3d':  ('1w', '1w'),
    '1w':  ('1w', '1w'),
}

# Weight each rung carries when measuring agreement. The higher timeframe leads:
# a 15m setup against a bearish 4h is a much bigger problem than the reverse.
ROLE_WEIGHTS: Dict[str, float] = {TREND: 0.45, STRUCTURE: 0.35, ENTRY: 0.20}


@dataclass(frozen=True)
class TimeframeLadder:
    """The resolved set of timeframes to analyse for one user selection."""

    selected: str
    rungs: Tuple[Tuple[str, str], ...]      # ((role, timeframe), …) high → low

    @property
    def timeframes(self) -> Tuple[str, ...]:
        return tuple(tf for _, tf in self.rungs)

    @property
    def is_multi(self) -> bool:
        return len(self.rungs) > 1


@dataclass
class TimeframeView:
    """One analysed rung."""

    role: str
    timeframe: str
    picture: TechnicalPicture

    @property
    def direction(self) -> str:
        return self.picture.trend_direction

    @property
    def conviction(self) -> float:
        return self.picture.trend_conviction


@dataclass
class MultiTimeframePicture:
    """The complete multi-timeframe read for one analysis request."""

    symbol: str
    market: str
    provider: str
    selected_timeframe: str
    ladder: TimeframeLadder
    views: List[TimeframeView] = field(default_factory=list)
    degraded_rungs: List[str] = field(default_factory=list)

    # ── The user-facing timeframe ──

    @property
    def entry(self) -> TechnicalPicture:
        """The picture for the timeframe the user actually selected."""
        return self.view(ENTRY).picture

    def view(self, role: str) -> TimeframeView:
        """The view for a role, falling back to the entry rung when absent."""
        for candidate in self.views:
            if candidate.role == role:
                return candidate
        return next(v for v in self.views if v.role == ENTRY)

    def has_role(self, role: str) -> bool:
        return any(v.role == role for v in self.views)

    # ── Agreement across timeframes ──

    @property
    def higher_timeframe_direction(self) -> str:
        """The trend rung's direction — the market's overall bias."""
        return self.view(TREND).direction

    @property
    def structure_direction(self) -> str:
        """The structure rung's direction."""
        return self.view(STRUCTURE).direction

    @property
    def aligned_direction(self) -> str:
        """The weighted-majority direction across every analysed rung."""
        scores = {BULLISH: 0.0, BEARISH: 0.0}
        for view in self.views:
            if view.direction in scores:
                scores[view.direction] += ROLE_WEIGHTS.get(view.role, 0.2)
        if scores[BULLISH] > scores[BEARISH]:
            return BULLISH
        if scores[BEARISH] > scores[BULLISH]:
            return BEARISH
        return RANGE

    @property
    def alignment(self) -> float:
        """Share of rung weight agreeing with the majority direction, 0–1.

        A rung reading RANGE neither agrees nor disagrees — it simply withholds
        its weight, which lowers alignment without counting as a conflict.
        """
        direction = self.aligned_direction
        if direction == RANGE:
            return 0.0
        total = sum(ROLE_WEIGHTS.get(v.role, 0.2) for v in self.views)
        if total <= 0:
            return 0.0
        agreeing = sum(
            ROLE_WEIGHTS.get(v.role, 0.2) for v in self.views if v.direction == direction
        )
        return round(agreeing / total, 4)

    def agreement_with(self, direction: str) -> float:
        """Share of rung weight agreeing with a specific trade direction, 0–1."""
        wanted = BULLISH if direction == 'long' else BEARISH
        total = sum(ROLE_WEIGHTS.get(v.role, 0.2) for v in self.views)
        if total <= 0:
            return 0.0
        agreeing = sum(
            ROLE_WEIGHTS.get(v.role, 0.2) for v in self.views if v.direction == wanted
        )
        return round(agreeing / total, 4)

    @property
    def conflicts(self) -> List[str]:
        """Rungs that point the opposite way to the majority."""
        direction = self.aligned_direction
        if direction == RANGE:
            return [
                f'{v.role} ({v.timeframe}) has no clear direction'
                for v in self.views if v.direction == RANGE
            ]
        opposite = BEARISH if direction == BULLISH else BULLISH
        return [
            f'{v.role} ({v.timeframe}) is {opposite} against a {direction} majority'
            for v in self.views if v.direction == opposite
        ]

    def opposes(self, direction: str) -> List[str]:
        """Rungs that contradict a proposed trade direction."""
        wanted = BULLISH if direction == 'long' else BEARISH
        opposite = BEARISH if wanted == BULLISH else BULLISH
        return [
            f'{v.role} timeframe is {opposite}'
            for v in self.views if v.direction == opposite
        ]


def build_ladder(selected: str, available: Tuple[str, ...]) -> TimeframeLadder:
    """Resolve the internal timeframe ladder for a user selection.

    Rungs that duplicate the selected timeframe, or that the provider does not
    offer, are dropped — so a venue with a short timeframe list still analyses
    everything it can.
    """
    offered = set(available)
    trend_tf, structure_tf = TIMEFRAME_LADDER.get(selected, (selected, selected))

    rungs: List[Tuple[str, str]] = []
    seen = {selected}

    if structure_tf in offered and structure_tf not in seen:
        rungs.append((STRUCTURE, structure_tf))
        seen.add(structure_tf)
    if trend_tf in offered and trend_tf not in seen:
        rungs.append((TREND, trend_tf))
        seen.add(trend_tf)

    # Order high → low so the trend rung is analysed (and logged) first, with
    # the user's own timeframe always last.
    rungs.sort(key=lambda r: timeframe_seconds(r[1]), reverse=True)
    rungs.append((ENTRY, selected))
    return TimeframeLadder(selected=selected, rungs=tuple(rungs))


class MultiTimeframeEngine:
    """Analyses every rung of the ladder and assembles the combined picture."""

    def __init__(
        self, settings, analysis_engine: Optional[AnalysisEngine] = None
    ) -> None:
        self.settings = settings
        self.analysis_engine = analysis_engine or AnalysisEngine(settings)

    def build(
        self,
        provider: MarketDataProvider,
        symbol: str,
        timeframe: str,
        scope: Optional[RequestScope] = None,
    ) -> MultiTimeframePicture:
        """Analyse the ladder for a symbol.

        Raises:
            ValueError / ProviderError: Only when the ENTRY rung fails. Higher
            rungs degrade silently into `degraded_rungs`.
        """
        scope = scope or RequestScope()
        selected = provider.ensure_timeframe(timeframe)
        ladder = build_ladder(selected, tuple(provider.supported_timeframes()))

        views: List[TimeframeView] = []
        degraded: List[str] = []

        for role, rung_tf in ladder.rungs:
            is_entry = role == ENTRY
            try:
                picture = self.analysis_engine.analyze(
                    provider, symbol, rung_tf,
                    scope=scope,
                    # Systemic benchmark context is a higher-timeframe concept;
                    # fetch it once, on the highest rung available.
                    include_benchmark=(role == TREND or (is_entry and not views)),
                    # The live quote only matters where the trade is taken.
                    include_quote=is_entry,
                )
            except (ValueError, ProviderError) as exc:
                if is_entry:
                    raise
                degraded.append(f'{role} ({rung_tf}): {exc}')
                logger.info(
                    'MTF degraded | %s %s rung %s unavailable: %s',
                    symbol, selected, rung_tf, exc,
                )
                continue
            views.append(TimeframeView(role=role, timeframe=rung_tf, picture=picture))

        entry_view = next(v for v in views if v.role == ENTRY)
        return MultiTimeframePicture(
            symbol=symbol,
            market=entry_view.picture.market,
            provider=entry_view.picture.provider,
            selected_timeframe=selected,
            ladder=ladder,
            views=views,
            degraded_rungs=degraded,
        )
