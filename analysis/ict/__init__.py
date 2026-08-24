"""ICT + MSNR Contextual Analysis Layers (Phase 2A).

Independent contextual/confirmation modules that INTERPRET existing analysis
through an ICT/MSNR lens.  They never duplicate the computations that
``analysis.structure``, ``analysis.levels``, or ``analysis/smc/`` already
perform — instead they reuse the same swings, candles, and indicators and
add higher-level interpretive context:

  msnr          Major Support/Nearest Resistance location engine
  displacement  Directional displacement detection
  premium_discount  Dealing-range position (premium/equilibrium/discount)
  ict_structure     ICT interpretation of existing structure (MSS/BOS adapter)
  ict_confluence    Relationship-based confluence across ICT + MSNR + existing

Every module is pure and network-free.  The ``ict_confluence`` module is the
integration point: it reads the outputs of every other module and produces a
single ``IctMsnrConfluence`` result that the main Confluence Engine consumes
as ONE additional contextual input — never as a second set of weighted votes.

DOUBLE-COUNTING PREVENTION:
  Each detected evidence item is wrapped in an ``EvidenceItem`` with a
  deterministic ``evidence_id`` built from (source, kind, price).  The ICT
  confluence deduplicates across modules before scoring, so the same swing
  high cannot contribute as both an MSNR resistance level AND a liquidity
  pool AND an ICT structure event.
"""

from analysis.ict.msnr import MSNREngine, MSNRResult
from analysis.ict.displacement import DisplacementEngine, DisplacementResult
from analysis.ict.premium_discount import PremiumDiscountEngine, PremiumDiscountResult
from analysis.ict.ict_structure import ICTStructureAdapter, ICTStructureResult
from analysis.ict.ict_confluence import ICTMSNRConfluenceEngine, IctMsnrConfluence
from analysis.ict.evidence import EvidenceItem, EvidenceRegistry

__all__ = [
    'MSNREngine',
    'MSNRResult',
    'DisplacementEngine',
    'DisplacementResult',
    'PremiumDiscountEngine',
    'PremiumDiscountResult',
    'ICTStructureAdapter',
    'ICTStructureResult',
    'ICTMSNRConfluenceEngine',
    'IctMsnrConfluence',
    'EvidenceItem',
    'EvidenceRegistry',
]
