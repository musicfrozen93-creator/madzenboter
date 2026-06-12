"""
ZenGrid V2 — Account Profiles.

V1 silently degraded small accounts: exchange min-notional floors inflated
entries to 1.5–4× the designed percentage while the recovery path (which
does not size up) silently skipped layers — small accounts got oversized
entries AND a disabled recovery system.

V2 makes the strategy variant an explicit design choice per account size:

  FULL    — standard 4-layer ladder, standard spacing.
  COMPACT — 3-layer ladder, wider spacing (each layer individually
            min-notional viable).
  MICRO   — entry + 1 wide recovery layer; the two-step variant designed
            for accounts where exchange floors dominate lot economics.

All profiles share the same PERCENTAGE risk budget — small accounts run a
coherent small version of the strategy, not a broken fraction of the big
one.
"""

import logging
from enum import Enum

from config.settings import Settings

logger = logging.getLogger(__name__)


class AccountProfile(str, Enum):
    """Account-size strategy variant."""
    FULL = 'full'
    COMPACT = 'compact'
    MICRO = 'micro'


def classify_account_profile(balance: float, settings: Settings) -> AccountProfile:
    """Classify an account into a strategy profile by balance.

    Args:
        balance: Current account balance in USDT.
        settings: Application settings (profile thresholds).

    Returns:
        AccountProfile for the balance.
    """
    if balance < settings.micro_account_max_balance:
        return AccountProfile.MICRO
    if balance < settings.compact_account_max_balance:
        return AccountProfile.COMPACT
    return AccountProfile.FULL


def get_profile_policy(profile: AccountProfile, settings: Settings) -> dict:
    """Ladder policy for a profile.

    Args:
        profile: The account profile.
        settings: Application settings (profile_policies dict).

    Returns:
        Dict with keys: max_layers (int), spacing_multiplier (float).
    """
    defaults = {'max_layers': 4, 'spacing_multiplier': 1.0}
    policy = settings.profile_policies.get(profile.value, {})
    return {**defaults, **policy}
