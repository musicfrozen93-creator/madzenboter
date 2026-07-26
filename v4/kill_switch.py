"""Unified Emergency Kill Switch (spec §A9, §C4, §C5).

Replaces V1's two disconnected emergency systems (BotControl.emergency_stop and
the never-triggered RiskManager.emergency_shutdown latch) with ONE authority, at
two scopes:

  • Global  — affects every account (platform-wide incident).
  • Account — affects one account (per-user pause / protection lock).

Three levels, increasing severity:
  • NORMAL   — trading permitted.
  • HALT_NEW — block NEW entries; keep managing/protecting open positions.
  • FLATTEN  — close everything and stop (full stop; manual clear).

The effective level for an account is the STRICTER of the global and per-account
levels. State is persisted through the supplied store so the kill switch survives
a restart (a halted platform must stay halted until explicitly cleared).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

NORMAL = 'normal'
HALT_NEW = 'halt_new'
FLATTEN = 'flatten'

_SEVERITY = {NORMAL: 0, HALT_NEW: 1, FLATTEN: 2}
_VALID = set(_SEVERITY)


def _valid(level: str) -> str:
    if level not in _VALID:
        raise ValueError(f'invalid kill-switch level: {level!r}')
    return level


@dataclass(frozen=True)
class KillSnapshot:
    global_level: str
    global_reason: str
    account_levels: Dict[int, str]


class KillSwitch:
    def __init__(self, store) -> None:
        self._store = store   # raw get_state/set_state (DB-backed in prod)

    # ── global ──
    def engage_global(self, level: str, reason: str) -> None:
        self._store.set_state('v4_kill_global_level', _valid(level))
        self._store.set_state('v4_kill_global_reason', reason or '')

    def clear_global(self) -> None:
        self._store.set_state('v4_kill_global_level', NORMAL)
        self._store.set_state('v4_kill_global_reason', '')

    def global_level(self) -> str:
        return self._store.get_state('v4_kill_global_level') or NORMAL

    # ── per-account ──
    def engage_account(self, account_id: int, level: str, reason: str) -> None:
        self._store.set_state(f'v4_kill_acct_{account_id}_level', _valid(level))
        self._store.set_state(f'v4_kill_acct_{account_id}_reason', reason or '')

    def clear_account(self, account_id: int) -> None:
        self._store.set_state(f'v4_kill_acct_{account_id}_level', NORMAL)
        self._store.set_state(f'v4_kill_acct_{account_id}_reason', '')

    def account_level(self, account_id: int) -> str:
        return self._store.get_state(f'v4_kill_acct_{account_id}_level') or NORMAL

    # ── effective decisions ──
    def effective_level(self, account_id: int) -> str:
        g = self.global_level()
        a = self.account_level(account_id)
        return g if _SEVERITY[g] >= _SEVERITY[a] else a

    def can_open(self, account_id: int) -> bool:
        """New entries permitted only at NORMAL."""
        return self.effective_level(account_id) == NORMAL

    def must_flatten(self, account_id: int) -> bool:
        return self.effective_level(account_id) == FLATTEN

    @staticmethod
    def can_manage() -> bool:
        """Management/protection ALWAYS runs — even a flatten IS management."""
        return True

    def snapshot(self) -> KillSnapshot:
        return KillSnapshot(
            global_level=self.global_level(),
            global_reason=self._store.get_state('v4_kill_global_reason') or '',
            account_levels={},
        )
