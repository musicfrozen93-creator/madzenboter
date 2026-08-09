"""
Zentry — Data Transfer Objects.

Lightweight in-memory objects produced by the market-analysis layer, separate
from the SQLAlchemy ORM models in core.models.

The position-lifecycle DTOs (RecoveryLayer, Basket, TradeRecord) and the
watchlist DTO (CoinScore) existed only to track automatically opened positions
and were removed in the Phase 0 conversion.
"""

import time
from dataclasses import dataclass, field


@dataclass
class Signal:
    """A market setup produced by the analysis layer.

    Field names are preserved for DB compatibility with the ``signals`` table:
      • ``market_regime`` stores the BTC regime string (bullish/bearish/neutral)
      • ``ema200`` stores the Bollinger middle band (SMA period)
      • ``volatility`` stores a coarse ATR label
    """

    symbol: str
    side: str  # 'long' or 'short'
    strength: float  # 0.0 – 1.0 signal quality
    atr: float
    market_regime: str  # BTC regime string (stored in signals.market_regime)
    volatility: str  # coarse label (stored in signals.volatility)
    current_price: float
    ema200: float  # Bollinger middle band (stored in signals.ema200)
    rsi: float
    bb_lower: float = 0.0
    bb_upper: float = 0.0
    reason: str = ''  # human-readable explanation of the setup
    strength_score: int = 0  # confluence score (0–4)
    timestamp: float = field(default_factory=time.time)
