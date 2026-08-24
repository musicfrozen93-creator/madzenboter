"""Evidence deduplication layer — prevents double-counting (Phase 2A).

Every piece of structural evidence detected across modules is wrapped in an
``EvidenceItem`` with a deterministic ``evidence_id``.  The ``EvidenceRegistry``
collects them and deduplicates so the same market event — the same swing high,
the same BOS, the same support level — is never scored more than once even when
it appears in the existing Structure Engine, the MSNR Location Engine, the ICT
Structure Adapter, AND the Liquidity Engine simultaneously.

Rules:
  • Two evidence items with the same ``evidence_id`` are the SAME event.
  • The registry keeps the one with the highest ``weight``.
  • Confluence scoring iterates the deduplicated registry, not the raw lists.

The id is built from (source_module, kind, canonical_price) so that:
  ┌─────────────────┬───────────────────────────────┬───────────────────┐
  │ Existing Engine  │ Detects swing high at 42000   │ id = SH:42000     │
  │ MSNR Engine      │ Detects resistance at 42000   │ id = SH:42000     │
  │ ICT Liquidity    │ Detects BSL pool at 42000     │ id = SH:42000     │
  └─────────────────┴───────────────────────────────┴───────────────────┘
  → ONE entry in the registry, not three.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class EvidenceItem:
    """One piece of market-structure evidence."""

    evidence_id: str           # deterministic key for dedup
    kind: str                  # 'swing_high' | 'swing_low' | 'bos' | 'mss' | 'support' | 'resistance' | 'liquidity_sweep' | 'displacement' | 'fvg' | 'order_block'
    direction: str             # 'bullish' | 'bearish' | 'neutral'
    price: float               # the canonical price level
    strength: float            # 0–1
    source: str                # which module originally detected it
    detail: str                # human-readable explanation


def make_evidence_id(kind: str, price: float, precision: int = 4) -> str:
    """Build a deterministic id from kind + rounded price.

    The rounding ensures that a swing high at 42000.12 and a resistance
    cluster centred at 42000.15 resolve to the same id when they are the
    same structural event, given a reasonable ATR.
    """
    return f'{kind}:{round(price, precision)}'


class EvidenceRegistry:
    """Deduplicated container of evidence items.

    When two items share an ``evidence_id``, the stronger one (higher
    ``strength``) wins.  The weaker one is recorded as ``suppressed`` so
    diagnostics can show what was merged.
    """

    def __init__(self) -> None:
        self._items: Dict[str, EvidenceItem] = {}
        self._suppressed: List[EvidenceItem] = []

    def add(self, item: EvidenceItem) -> None:
        existing = self._items.get(item.evidence_id)
        if existing is None:
            self._items[item.evidence_id] = item
        elif item.strength > existing.strength:
            self._suppressed.append(existing)
            self._items[item.evidence_id] = item
        else:
            self._suppressed.append(item)

    def add_all(self, items: List[EvidenceItem]) -> None:
        for item in items:
            self.add(item)

    @property
    def items(self) -> List[EvidenceItem]:
        """Deduplicated evidence, strongest retained per id."""
        return list(self._items.values())

    @property
    def suppressed(self) -> List[EvidenceItem]:
        """Items that were merged away (for diagnostics)."""
        return list(self._suppressed)

    def bullish(self) -> List[EvidenceItem]:
        return [e for e in self.items if e.direction == 'bullish']

    def bearish(self) -> List[EvidenceItem]:
        return [e for e in self.items if e.direction == 'bearish']

    def neutral(self) -> List[EvidenceItem]:
        return [e for e in self.items if e.direction == 'neutral']

    def by_kind(self, kind: str) -> List[EvidenceItem]:
        return [e for e in self.items if e.kind == kind]

    @property
    def dedup_count(self) -> int:
        """How many items were suppressed by deduplication."""
        return len(self._suppressed)
