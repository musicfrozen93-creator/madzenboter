"""Provider registry — the single place a market maps to an implementation.

Adding a market is a two-line change here plus one new `MarketDataProvider`
subclass. Nothing in `analysis/` or `api/` is touched:

    from providers.bybit import BybitProvider
    register(BybitProvider)

Providers are constructed lazily and initialized once, then cached for the
process lifetime — `initialize()` loads venue metadata and is expensive.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Dict, Optional, Type

from config.settings import Settings
from providers.base import MarketDataProvider, ProviderError

logger = logging.getLogger(__name__)

# Provider classes keyed by their `name`.
_REGISTRY: Dict[str, Type[MarketDataProvider]] = {}

# The provider used when a request names a market but not a specific provider.
_DEFAULT_FOR_MARKET: Dict[str, str] = {}

# Initialized singletons keyed by provider name.
_INSTANCES: Dict[str, MarketDataProvider] = {}
_LOCK = threading.Lock()


class UnknownProviderError(ProviderError):
    """No provider is registered under that name, or for that market."""


def register(
    provider_cls: Type[MarketDataProvider], *, default_for_market: bool = True
) -> Type[MarketDataProvider]:
    """Register a provider class.

    Args:
        provider_cls: A concrete MarketDataProvider subclass with `name` and
            `market` set.
        default_for_market: Make this the provider used when a request names
            only the market. The first registration for a market always wins
            unless a later one passes this explicitly.

    Returns:
        The class, so this can also be used as a decorator.
    """
    name = provider_cls.name
    market = provider_cls.market
    if not name or not market:
        raise ValueError(
            f'{provider_cls.__name__} must set both `name` and `market`'
        )
    _REGISTRY[name] = provider_cls
    if default_for_market or market not in _DEFAULT_FOR_MARKET:
        _DEFAULT_FOR_MARKET[market] = name
    logger.debug('Registered provider %s for market %s', name, market)
    return provider_cls


def resolve_name(market: str, provider: Optional[str] = None) -> str:
    """Resolve (market, optional provider) to a registered provider name.

    Raises:
        UnknownProviderError: If the market or the named provider is unknown,
            or the named provider does not serve that market.
    """
    market = (market or '').strip().lower()
    if provider:
        key = provider.strip().lower()
        cls = _REGISTRY.get(key)
        if cls is None:
            raise UnknownProviderError(
                f'Unknown provider {provider!r}. Available: {", ".join(sorted(_REGISTRY))}'
            )
        if market and cls.market != market:
            raise UnknownProviderError(
                f'Provider {key!r} serves the {cls.market!r} market, not {market!r}'
            )
        return key

    name = _DEFAULT_FOR_MARKET.get(market)
    if name is None:
        raise UnknownProviderError(
            f'No provider for market {market!r}. Available markets: '
            f'{", ".join(sorted(_DEFAULT_FOR_MARKET))}'
        )
    return name


def get_provider(
    settings: Settings, market: str, provider: Optional[str] = None
) -> MarketDataProvider:
    """Return an initialized provider for a market.

    Thread-safe and idempotent: the first caller pays for `initialize()`, every
    later caller gets the same instance.
    """
    name = resolve_name(market, provider)
    instance = _INSTANCES.get(name)
    if instance is not None:
        return instance

    with _LOCK:
        instance = _INSTANCES.get(name)
        if instance is None:
            instance = _REGISTRY[name](settings)
            instance.initialize()
            _INSTANCES[name] = instance
            logger.info('Initialized provider %s (%s)', name, instance.market)
    return instance


def available_markets() -> dict[str, list[str]]:
    """Map each registered market to the provider names serving it."""
    markets: dict[str, list[str]] = {}
    for name, cls in sorted(_REGISTRY.items()):
        markets.setdefault(cls.market, []).append(name)
    return markets


def describe_providers() -> list[dict]:
    """Capability summary of every registered provider.

    Reads class attributes only — no provider is constructed and no network
    call is made, so discovery stays cheap even when a venue is down.
    """
    return [
        {
            'provider': name,
            'market': cls.market,
            'timeframes': list(cls.timeframes),
            'is_default': _DEFAULT_FOR_MARKET.get(cls.market) == name,
        }
        for name, cls in sorted(_REGISTRY.items())
    ]


def default_provider_for(market: str) -> Optional[str]:
    """The provider name used when a request omits `provider`."""
    return _DEFAULT_FOR_MARKET.get((market or '').strip().lower())


def shutdown() -> None:
    """Close and drop every initialized provider (called on service shutdown)."""
    with _LOCK:
        for name, instance in _INSTANCES.items():
            try:
                instance.close()
            except Exception as exc:
                logger.warning('Provider %s failed to close cleanly: %s', name, exc)
        _INSTANCES.clear()


def reset_for_tests() -> None:
    """Drop cached instances without touching registrations."""
    with _LOCK:
        _INSTANCES.clear()


def _install_builtin_providers(_register: Callable = register) -> None:
    """Register the providers that ship with the platform.

    Imported here rather than at module top level so the registry module itself
    stays dependency-free and importable in isolation.

    Crypto (Binance) is always available. Forex (Twelve Data) is registered ONLY
    when a forex data key is configured, so a deployment without one simply does
    not offer the forex market instead of failing at request time.
    """
    from providers.binance import BinanceProvider

    _register(BinanceProvider)

    if os.environ.get('TWELVEDATA_API_KEY'):
        from providers.twelvedata import TwelveDataForexProvider
        _register(TwelveDataForexProvider)


_install_builtin_providers()
