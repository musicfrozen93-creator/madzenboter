"""Research extension point — the shape a future strategy validation must take.

WHAT THIS IS
    A structured record of one generated signal, carrying everything a later
    backtest or walk-forward study needs to attribute a result to the exact
    strategy definition that produced it, plus the vocabulary that validation
    must report in.

WHAT THIS IS NOT
    A backtester, and not a source of performance figures. This project has no
    backtesting framework, so no strategy here has measured performance. Nothing
    in this module computes, estimates or stores a win rate, an expectancy or a
    return, and nothing that consumes it may present one. A strategy stays
    ``experimental`` until real out-of-sample evidence exists.

WHY IT EXISTS NOW
    The four non-ICT strategies are compositions over the module registry rather
    than validated methodologies. Recording their signals in a consistent shape
    from the start is what makes it possible to validate them later without
    re-running history — and it keeps the honest answer ("not measured") a
    structural fact rather than a note in a document.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

# ── The metrics a validation must report ────────────────────────────────────
# Named here so a future study reports a complete picture rather than the
# flattering subset. Every one of these is currently UNMEASURED for every
# strategy in this codebase.
REQUIRED_METRICS: Tuple[str, ...] = (
    'sample_size',
    'win_rate',
    'average_r',
    'expectancy',
    'profit_factor',
    'max_drawdown',
    'tp1_hit_rate',
    'tp2_hit_rate',
    'tp3_hit_rate',
    'sl_rate',
    'average_holding_time',
    'signal_frequency',
)

#: Dimensions a validation must break those metrics down by — an aggregate that
#: hides a market or regime where the strategy fails is not a validation.
REQUIRED_BREAKDOWNS: Tuple[str, ...] = ('market', 'timeframe', 'regime')

#: Methodological conditions a study must satisfy before its numbers may be
#: used to promote a strategy from experimental to production.
REQUIRED_CONDITIONS: Tuple[str, ...] = (
    'out_of_sample',        # tested on data not used to design the strategy
    'walk_forward',         # rolling re-fit, not one fixed split
    'realistic_fees',
    'realistic_spread',
    'realistic_slippage',
    'no_look_ahead',        # no bar the strategy could not have seen
)


@dataclass(frozen=True)
class SignalRecord:
    """One generated signal, in the shape a later study can consume.

    ``outcome`` is deliberately Optional and defaults to None: the result of a
    trade is not known when the signal is generated, and inventing one is the
    single most dangerous thing this module could do.
    """

    strategy_id: str
    strategy_version: int
    strategy_status: str

    market: str
    symbol: str
    timeframe: str

    direction: str                       # 'BUY' | 'SELL' | 'WAIT'
    tradeable: bool
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profits: Tuple[float, ...] = ()

    quality: int = 0
    confidence: int = 0
    risk_reward: Optional[float] = None
    regime: Optional[str] = None

    generated_at: Optional[str] = None

    #: Filled in later by an outcome tracker, never at generation time.
    outcome: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def record_from_result(result, strategy) -> SignalRecord:
    """Build a :class:`SignalRecord` from a completed pipeline result.

    Pure and side-effect free — it stores nothing and claims nothing. Callers
    decide whether to persist the record.
    """
    signal = result.signal
    entry_picture = result.mtf.entry
    return SignalRecord(
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version,
        strategy_status=strategy.status,
        market=signal.market,
        symbol=signal.symbol,
        timeframe=signal.timeframe,
        direction=signal.direction,
        tradeable=signal.tradeable,
        entry=signal.entry,
        stop_loss=signal.stop_loss,
        take_profits=tuple(signal.take_profits),
        quality=result.quality.value,
        confidence=result.confidence.value,
        risk_reward=signal.risk_reward,
        regime=getattr(getattr(entry_picture, 'regime', None), 'label', None),
        generated_at=getattr(signal, 'generated_at', None),
    )


@dataclass(frozen=True)
class ValidationReport:
    """The result of a strategy validation study.

    Constructing one with ``metrics={}`` is the correct representation of "not
    yet measured" — which is the state of every strategy in this codebase.
    ``is_sufficient`` is what a promotion decision should consult; it refuses
    anything that has not reported every required metric, breakdown and
    methodological condition.
    """

    strategy_id: str
    strategy_version: int
    metrics: dict = field(default_factory=dict)
    breakdowns: dict = field(default_factory=dict)
    conditions: dict = field(default_factory=dict)
    notes: str = ''

    @property
    def missing_metrics(self) -> List[str]:
        return [m for m in REQUIRED_METRICS if m not in self.metrics]

    @property
    def missing_breakdowns(self) -> List[str]:
        return [b for b in REQUIRED_BREAKDOWNS if b not in self.breakdowns]

    @property
    def unmet_conditions(self) -> List[str]:
        return [c for c in REQUIRED_CONDITIONS if not self.conditions.get(c)]

    @property
    def is_sufficient(self) -> bool:
        """True only when the study is complete enough to justify promotion.

        Deliberately strict: an incomplete study must never be able to move a
        strategy to production status.
        """
        return not (
            self.missing_metrics or self.missing_breakdowns or self.unmet_conditions
        )


def unvalidated(strategy_id: str, strategy_version: int = 1) -> ValidationReport:
    """The honest report for a strategy nothing has measured yet."""
    return ValidationReport(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        notes='No backtest or walk-forward study has been run for this strategy.',
    )
