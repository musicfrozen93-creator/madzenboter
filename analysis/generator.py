"""Signal Generator — stage 3 of the signal pipeline.

Turns the confluence read into the concrete numbers a user acts on: a direction,
an entry, a stop, and the TP1/TP2/TP3 ladder.

Every level comes from the analysis, never from a fixed multiplier:

    Entry   current price, snapped to a structural level when price is already
            retesting one
    Stop    beyond the protective swing / support-resistance zone that would
            invalidate the idea, plus an ATR buffer against wick hunts
    TP1-3   the nearest opposing S/R zones, Fibonacci extensions of the dominant
            leg, and the structural measured move — deduplicated, ordered, and
            filtered by the risk-reward they actually produce

WAIT is a real answer. The generator returns WAIT — never a fabricated trade —
when market conditions are unclean, when the evidence is two-sided, when a
higher timeframe contradicts the setup, when quality is below the tradeable
floor, or when no stop can be placed at acceptable risk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from analysis.confluence import ConfluenceResult
from analysis.engine import TechnicalPicture
from analysis.scoring import MIN_TRADEABLE_CONFIDENCE, MIN_TRADEABLE_QUALITY, Score
from analysis.structure import BULLISH
from analysis.timeframes import MultiTimeframePicture
from v4.params import V4Params
from v4.trade_math import compute_stop, take_profit_levels

logger = logging.getLogger(__name__)

BUY = 'BUY'
SELL = 'SELL'
WAIT = 'WAIT'

# Price is treated as "retesting" a level when it sits within this much ATR of it.
RETEST_ATR_TOLERANCE = 0.25

# Buffer placed beyond the invalidation level so ordinary wicks do not trigger it.
STOP_BUFFER_ATR = 0.5

# Target candidates closer together than this (in ATR) are the same level.
TARGET_MERGE_ATR = 0.3

# TP1 must be worth taking; the full ladder must clear the minimum reward:risk.
MIN_TP1_RR = 0.5


@dataclass
class TradingSignal:
    """One complete signal, ready to be serialized to the dashboard."""

    market: str
    provider: str
    symbol: str
    timeframe: str
    direction: str                      # 'BUY' | 'SELL' | 'WAIT'
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profits: List[float] = field(default_factory=list)
    risk_reward: Optional[float] = None
    risk_pct: Optional[float] = None
    rr_per_tp: List[float] = field(default_factory=list)
    target_sources: List[str] = field(default_factory=list)
    entry_basis: Optional[str] = None
    stop_basis: Optional[str] = None
    wait_reason: Optional[str] = None

    @property
    def actionable(self) -> bool:
        """True when the signal carries a tradeable direction and levels."""
        return self.direction in (BUY, SELL) and self.entry is not None


@dataclass(frozen=True)
class _TargetCandidate:
    price: float
    source: str


class SignalGenerator:
    """Builds a TradingSignal from the multi-timeframe read."""

    def __init__(self, params: Optional[V4Params] = None) -> None:
        self.params = params or V4Params()

    def generate(
        self,
        mtf: MultiTimeframePicture,
        confluence: ConfluenceResult,
        quality: Score,
        confidence: Score,
        price_precision: int = 8,
    ) -> TradingSignal:
        """Produce the signal, or a WAIT carrying the reason it was withheld."""
        entry_picture = mtf.entry
        base = dict(
            market=mtf.market,
            provider=mtf.provider,
            symbol=mtf.symbol,
            timeframe=mtf.selected_timeframe,
        )

        def wait(reason: str) -> TradingSignal:
            return TradingSignal(direction=WAIT, wait_reason=reason, **base)

        # 1. Unclean conditions: a reading taken off a news candle or a blown-out
        #    spread is not trustworthy, whatever the modules say.
        blocking = entry_picture.blocking_condition
        if blocking:
            return wait(f'market conditions unsuitable — {blocking}')

        # 2. Two-sided or contradicted evidence.
        if confluence.hard_conflicts:
            return wait('conflicting signals — ' + '; '.join(confluence.hard_conflicts))
        if not confluence.has_direction:
            return wait(confluence.reason or 'no directional consensus')

        side = confluence.trade_side
        assert side is not None

        # 3. Quality floor. Signal quality matters more than signal quantity.
        if quality.value < MIN_TRADEABLE_QUALITY:
            return wait(
                f'setup quality {quality.value}/100 ({quality.grade}) is below the '
                f'tradeable floor of {MIN_TRADEABLE_QUALITY}'
            )

        # 4. Confidence floor. The engine must be reasonably certain about its own
        #    reading before it emits a tradeable signal.
        if confidence.value < MIN_TRADEABLE_CONFIDENCE:
            return wait(
                f'engine confidence {confidence.value}/100 ({confidence.grade}) is '
                f'below the tradeable floor of {MIN_TRADEABLE_CONFIDENCE}'
            )

        # 5. Levels, all derived from the analysis.
        entry, entry_basis = self._entry(entry_picture, side, price_precision)
        stop, stop_basis = self._stop(entry_picture, side, entry, price_precision)
        if stop is None:
            return wait(f'no valid stop level — {stop_basis}')

        risk = abs(entry - stop)
        risk_pct = risk / entry if entry > 0 else 0.0
        if risk_pct > self.params.stop_ceiling_pct:
            return wait(
                f'invalidation sits {risk_pct:.2%} away, beyond the '
                f'{self.params.stop_ceiling_pct:.0%} risk ceiling'
            )

        targets = self._targets(entry_picture, side, entry, risk, price_precision)
        if len(targets) < 3:
            return wait(
                'not enough structural targets above the entry to build a '
                'TP1/TP2/TP3 ladder'
            )

        # Per-target reward:risk for transparent evaluation.
        rr_per_tp = [
            round(abs(t.price - entry) / risk, 2) if risk > 0 else 0.0
            for t in targets
        ]

        # TP1 must offer a meaningful near-term reward, not just 0.5R.
        if rr_per_tp[0] < self.params.min_tp1_rr:
            return wait(
                f'TP1 reward:risk is {rr_per_tp[0]:.2f}, below the '
                f'{self.params.min_tp1_rr:.2f} minimum'
            )

        # The furthest target must clear the overall minimum reward:risk.
        minimum = take_profit_levels(entry, stop, side, self.params)['min_rr_price']
        clears = (
            targets[-1].price >= minimum if side == 'long' else targets[-1].price <= minimum
        )
        if not clears:
            return wait(
                f'best available reward:risk is {rr_per_tp[-1]:.2f}, below the '
                f'{self.params.min_rr:.2f} minimum'
            )

        return TradingSignal(
            direction=BUY if side == 'long' else SELL,
            entry=entry,
            stop_loss=stop,
            take_profits=[t.price for t in targets],
            risk_reward=round(rr_per_tp[-1], 2),
            risk_pct=round(risk_pct, 5),
            rr_per_tp=rr_per_tp,
            target_sources=[t.source for t in targets],
            entry_basis=entry_basis,
            stop_basis=stop_basis,
            **base,
        )

    # ── Entry ──

    def _entry(
        self, picture: TechnicalPicture, side: str, precision: int
    ) -> tuple[float, str]:
        """Entry price: the live price, or the level price is already retesting.

        When price sits within a quarter-ATR of the zone it is reacting to, the
        zone edge is the honest entry — that is where the trade is actually
        taken, not the last tick.
        """
        ind = picture.indicators
        price = picture.quote.last if picture.quote and picture.quote.last > 0 else ind.price
        tolerance = RETEST_ATR_TOLERANCE * ind.atr

        levels = picture.levels
        if tolerance > 0:
            if side == 'long' and levels.nearest_support:
                edge = levels.nearest_support.upper
                if 0 <= price - edge <= tolerance:
                    return _round(edge, precision), (
                        f'retest of support at {edge:.8f} '
                        f'({levels.nearest_support.touches} touches)'
                    )
            if side == 'short' and levels.nearest_resistance:
                edge = levels.nearest_resistance.lower
                if 0 <= edge - price <= tolerance:
                    return _round(edge, precision), (
                        f'retest of resistance at {edge:.8f} '
                        f'({levels.nearest_resistance.touches} touches)'
                    )

        return _round(price, precision), 'current market price'

    # ── Stop ──

    def _stop(
        self, picture: TechnicalPicture, side: str, entry: float, precision: int
    ) -> tuple[Optional[float], str]:
        """Stop beyond the level whose loss invalidates the setup, plus a buffer.

        Candidates, in order of preference:
          1. the protective swing from market structure
          2. the far edge of the nearest support/resistance zone
          3. the 0.786 retracement of the dominant Fibonacci leg
        The furthest valid candidate wins, because the stop must sit beyond
        EVERY level the idea depends on — not just the closest one.
        """
        ind = picture.indicators
        atr = ind.atr
        if atr <= 0:
            return None, 'ATR unavailable, cannot size a stop buffer'

        long = side == 'long'
        candidates: List[tuple[float, str]] = []

        structure = picture.structure
        protective = structure.protective_low if long else structure.protective_high
        if protective is not None:
            candidates.append((protective, 'protective swing'))

        levels = picture.levels
        zone = levels.nearest_support if long else levels.nearest_resistance
        if zone is not None:
            candidates.append((zone.lower if long else zone.upper, 'support/resistance zone'))

        fib = picture.fibonacci
        if fib.valid and '0.786' in fib.retracements:
            deep = fib.retracements['0.786']
            if (long and deep < entry) or (not long and deep > entry):
                candidates.append((deep, '0.786 Fibonacci retracement'))

        # Keep only candidates on the protective side of entry.
        valid = [
            (price, source) for price, source in candidates
            if (price < entry if long else price > entry)
        ]
        if not valid:
            return None, 'no structural level beyond the entry to protect against'

        anchor, source = (min(valid) if long else max(valid))

        # `compute_stop` owns the buffer, the noise floor and the risk ceiling —
        # this module only decides WHICH level to anchor to.
        result = compute_stop(
            entry=entry, side=side, atr=atr * STOP_BUFFER_ATR / self.params.stop_atr_mult,
            structural_level=anchor, params=self.params,
        )
        if not result.valid:
            return None, result.reason

        return _round(result.stop_price, precision), (
            f'{STOP_BUFFER_ATR:.1f} ATR beyond the {source} at {anchor:.8f} '
            f'({result.stop_pct:.2%} risk)'
        )

    # ── Targets ──

    def _targets(
        self,
        picture: TechnicalPicture,
        side: str,
        entry: float,
        risk: float,
        precision: int,
    ) -> List[_TargetCandidate]:
        """Build the TP1/TP2/TP3 ladder from real levels ahead of the trade."""
        long = side == 'long'
        atr = picture.indicators.atr
        candidates: List[_TargetCandidate] = []

        # 1. Opposing support/resistance zones — where price is most likely to
        #    stall, so the most honest first place to take profit.
        zones = picture.levels.resistances if long else picture.levels.supports
        for zone in zones:
            if (long and zone.center > entry) or (not long and zone.center < entry):
                candidates.append(_TargetCandidate(
                    zone.center,
                    f'{"resistance" if long else "support"} zone '
                    f'({zone.touches} touches)',
                ))

        # 2. Fibonacci extensions of the dominant leg.
        fib = picture.fibonacci
        if fib.valid:
            for ratio, price in fib.extensions.items():
                if (long and price > entry) or (not long and price < entry):
                    candidates.append(_TargetCandidate(
                        price, f'{ratio} Fibonacci extension'
                    ))

        # 3. Structural measured move — project the last swing range from entry.
        structure = picture.structure
        if structure.last_swing_high and structure.last_swing_low:
            span = structure.last_swing_high - structure.last_swing_low
            if span > 0:
                projected = entry + span if long else entry - span
                candidates.append(_TargetCandidate(projected, 'measured move'))

        # A level sitting almost on top of the entry is not a target — taking it
        # would mean risking 1R to make a fraction of one. Skip past those to the
        # next real destination rather than rejecting the setup over them.
        minimum_distance = MIN_TP1_RR * risk
        candidates = [
            c for c in candidates if abs(c.price - entry) >= minimum_distance
        ]

        # Order by distance from entry, then merge levels that are effectively
        # the same price so the ladder is three distinct destinations.
        candidates.sort(key=lambda c: abs(c.price - entry))
        merged: List[_TargetCandidate] = []
        merge_distance = TARGET_MERGE_ATR * atr if atr > 0 else 0.0
        for candidate in candidates:
            if any(abs(candidate.price - kept.price) <= merge_distance for kept in merged):
                continue
            merged.append(_TargetCandidate(_round(candidate.price, precision), candidate.source))
            if len(merged) == 3:
                break

        return merged


def _round(price: float, precision: int) -> float:
    """Round a level to the instrument's real tick precision."""
    return round(price, max(0, min(12, precision)))
