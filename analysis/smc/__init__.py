"""Smart Money Concepts — confirmation layers (Phase 2).

Seven additional analysis engines that raise or lower signal quality but never
create a signal on their own. Each is a pure, network-free function of the
already-fetched candles (and the swings the structure engine already found), so
they add computation but no new market-data fetches.

  order_blocks  institutional order-block zones and their lifecycle
  fvg           fair value gaps (price imbalances) and their fill state
  liquidity     equal highs/lows, pools, and sweeps/grabs
  vwap          anchored VWAP, price relationship, and slope
  macd          MACD line, signal, histogram, crosses
  adx           trend strength via ADX + DI (reuses the Phase 0 ADX)
  patterns      classic chart-pattern recognition
"""

from analysis.smc.adx import ADXState, compute_adx_state
from analysis.smc.fvg import FairValueGap, FairValueGapState, detect_fair_value_gaps
from analysis.smc.liquidity import LiquidityPool, LiquidityState, detect_liquidity
from analysis.smc.macd import MACDState, compute_macd
from analysis.smc.order_blocks import OrderBlock, OrderBlockState, detect_order_blocks
from analysis.smc.patterns import Pattern, PatternState, detect_patterns
from analysis.smc.vwap import VWAPState, compute_vwap

__all__ = [
    'ADXState',
    'FairValueGap',
    'FairValueGapState',
    'LiquidityPool',
    'LiquidityState',
    'MACDState',
    'OrderBlock',
    'OrderBlockState',
    'Pattern',
    'PatternState',
    'VWAPState',
    'compute_adx_state',
    'compute_macd',
    'compute_vwap',
    'detect_fair_value_gaps',
    'detect_liquidity',
    'detect_order_blocks',
    'detect_patterns',
]
