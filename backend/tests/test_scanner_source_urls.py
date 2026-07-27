"""Scanner endpoints are configuration, not code.

Every adapter has always accepted a ``url``, but the factory never passed one, so
pointing a source at a mirror, a proxy or a replacement API meant editing the
adapter. These tests pin the override path and — just as importantly — that the
defaults still apply when nothing is overridden.
"""

from __future__ import annotations

import pytest

from hades.contexts.scanner.infrastructure.sources.dexscreener import DexScreenerSource
from hades.contexts.scanner.infrastructure.sources.factory import SOURCE_REGISTRY, build_sources


def test_sources_keep_their_documented_defaults_when_nothing_is_overridden() -> None:
    (source,) = build_sources(["dexscreener"])

    assert isinstance(source, DexScreenerSource)
    assert source.url.startswith("https://api.dexscreener.com/")


def test_an_override_redirects_only_the_named_source() -> None:
    sources = build_sources(
        ["dexscreener", "raydium"],
        url_overrides={"dexscreener": "https://my-bridge.internal/dex"},
    )
    by_name = {s.name: s for s in sources}

    assert by_name["dexscreener"].url == "https://my-bridge.internal/dex"
    # Overriding one source must not disturb the others.
    assert by_name["raydium"].url.startswith("https://api-v3.raydium.io/")


def test_an_override_for_a_source_that_is_not_enabled_is_inert() -> None:
    (source,) = build_sources(["dexscreener"], url_overrides={"orca": "https://example.invalid"})

    assert source.name == "dexscreener"


def test_a_blank_override_falls_back_to_the_default() -> None:
    # An empty env value must not produce a source pointed at "".
    (source,) = build_sources(["dexscreener"], url_overrides={"dexscreener": ""})

    assert source.url.startswith("https://api.dexscreener.com/")


def test_an_unknown_source_name_is_skipped_not_fatal() -> None:
    sources = build_sources(["dexscreener", "not_a_real_dex"])

    assert [s.name for s in sources] == ["dexscreener"]


def test_every_registered_source_accepts_an_override() -> None:
    # The registry is the extension point; a new adapter that ignored ``url``
    # would silently reintroduce the hardcoded-endpoint problem.
    for name in SOURCE_REGISTRY:
        (source,) = build_sources([name], url_overrides={name: f"https://example.test/{name}"})
        assert source.url == f"https://example.test/{name}"


# -- endpoints verified against live hosts on 2026-07-26 ----------------------


def test_pumpfun_does_not_point_at_the_retired_host() -> None:
    # frontend-api.pump.fun answers Cloudflare 530 ("origin unreachable"); the
    # feed moved to frontend-api-v3. The old host left this source permanently
    # down, which is invisible in a scanner that tolerates a source failing.
    (source,) = build_sources(["pumpfun"])

    assert "frontend-api-v3.pump.fun" in source.url
    assert "//frontend-api.pump.fun" not in source.url


def test_raydium_uses_a_sort_field_the_api_actually_accepts() -> None:
    # Raydium v3 takes default/liquidity/volume24h/fee24h/apr24h — never
    # "created". It answers 500 (not 400) for an unknown field, so every poll
    # failed and the error read like an outage rather than a bad request.
    (source,) = build_sources(["raydium"])

    assert "poolSortField=created" not in source.url
    assert "poolSortField=default" in source.url


@pytest.mark.parametrize("name", sorted(SOURCE_REGISTRY))
def test_every_source_url_is_absolute_https(name: str) -> None:
    (source,) = build_sources([name])

    assert source.url.startswith("https://")
