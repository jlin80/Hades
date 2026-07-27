"""Source factory — build the configured discovery adapters by name.

The active sources are declared in ``SCANNER_SOURCES`` (e.g.
``pumpfun,raydium,dexscreener``). This factory maps each name to its adapter
class. Adding a new DEX means writing an adapter and registering it here — no
other code changes. Unknown names are skipped with a warning so a typo never
crashes discovery.
"""

from __future__ import annotations

from collections.abc import Mapping

from hades.contexts.scanner.domain.ports import TokenSource
from hades.contexts.scanner.infrastructure.sources.base import HttpPollingSource
from hades.contexts.scanner.infrastructure.sources.dexscreener import DexScreenerSource
from hades.contexts.scanner.infrastructure.sources.jupiter import JupiterSource
from hades.contexts.scanner.infrastructure.sources.meteora import MeteoraSource
from hades.contexts.scanner.infrastructure.sources.orca import OrcaSource
from hades.contexts.scanner.infrastructure.sources.pumpfun import PumpFunSource
from hades.contexts.scanner.infrastructure.sources.raydium import RaydiumSource
from hades.shared_kernel.logging import get_logger

_logger = get_logger("scanner.sources")

SOURCE_REGISTRY: dict[str, type[HttpPollingSource]] = {
    "pumpfun": PumpFunSource,
    "raydium": RaydiumSource,
    "dexscreener": DexScreenerSource,
    "orca": OrcaSource,
    "meteora": MeteoraSource,
    "jupiter": JupiterSource,
}


def build_sources(
    names: list[str],
    *,
    poll_interval_seconds: float = 5.0,
    timeout_seconds: float = 12.0,
    url_overrides: Mapping[str, str] | None = None,
) -> list[TokenSource]:
    """Instantiate the requested adapters, skipping unknown names.

    ``url_overrides`` maps a source name to a replacement endpoint. Each adapter
    already carries a working default; the override exists so pointing a source at
    a mirror, a proxy or a drop-in replacement API is a configuration change rather
    than a code edit — which is what it used to require, because this factory never
    passed the ``url`` its adapters have always accepted.

    An override only changes *where* the payload comes from; the adapter's parser
    is unchanged, so a substitute endpoint must still speak that source's shape.
    """
    overrides = dict(url_overrides or {})
    sources: list[TokenSource] = []
    for name in names:
        source_cls = SOURCE_REGISTRY.get(name)
        if source_cls is None:
            _logger.warning("unknown_scanner_source", source=name)
            continue
        kwargs: dict[str, object] = {
            "poll_interval_seconds": poll_interval_seconds,
            "timeout_seconds": timeout_seconds,
        }
        override = overrides.get(name)
        if override:
            kwargs["url"] = override
            _logger.info("scanner_source_url_overridden", source=name, url=override)
        sources.append(source_cls(**kwargs))  # type: ignore[arg-type]
    return sources
