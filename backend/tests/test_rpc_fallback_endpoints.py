"""``SOLANA_RPC_FALLBACK_URLS`` reaches the provider list.

The setting has existed since Phase 2 and is documented in ``.env.example`` as
"comma-separated backups". Nothing read it: ``build_endpoints`` took the primary
URL and stopped, so the platform had failover, health-scored ranking and a
documented way to configure spares — and exactly one provider.

On 2026-08-11 that provider began answering ``HTTP 500`` to ``getAccountInfo``
and ``getTokenLargestAccounts`` while ``getHealth`` kept succeeding. The public
RPC served the same call in 0.45 s. Every account read timed out at 12 s, the
Security assembler spent ~24 of its ~25 seconds waiting for two of them, holders
came back empty, the cluster detector had nothing to cluster, Intelligence
assembled empty batches, and the committee saw nothing.

``max_attempts`` was inert for the same reason: :meth:`RpcManager.call` iterates
ranked *candidates*, so one provider is one attempt whatever the retry budget
says. A spare is what makes both settings mean something.
"""

from __future__ import annotations

from hades.shared_kernel.config.settings import RpcEndpointConfig, RpcSettings
from hades.shared_kernel.solana.rpc_manager import build_endpoints

_PRIMARY = "https://primary.example/rpc"
_BACKUP_A = "https://backup-a.example/rpc"
_BACKUP_B = "https://backup-b.example/rpc"


def test_backups_join_the_provider_list() -> None:
    endpoints = build_endpoints(RpcSettings(), _PRIMARY, f"{_BACKUP_A},{_BACKUP_B}")

    assert [e.http_url for e in endpoints] == [_PRIMARY, _BACKUP_A, _BACKUP_B]


def test_backups_rank_below_the_primary() -> None:
    """The paid provider stays preferred; a spare is for when health has moved."""
    endpoints = build_endpoints(RpcSettings(), _PRIMARY, _BACKUP_A)

    primary, backup = endpoints
    assert primary.priority > backup.priority


def test_blank_and_whitespace_entries_are_ignored() -> None:
    """A trailing comma in a .env file must not create a provider with no URL."""
    endpoints = build_endpoints(RpcSettings(), _PRIMARY, f" {_BACKUP_A} , ,")

    assert [e.http_url for e in endpoints] == [_PRIMARY, _BACKUP_A]


def test_no_backups_is_still_a_single_provider() -> None:
    endpoints = build_endpoints(RpcSettings(), _PRIMARY, "")

    assert [e.http_url for e in endpoints] == [_PRIMARY]


def test_explicit_endpoints_still_win() -> None:
    """``RPC_ENDPOINTS`` is the fully-specified form and must not be diluted.

    An operator who has described their providers exactly — with names, priorities
    and rate limits — has said something more precise than a URL list, and quietly
    appending to it would change a ranking they set deliberately.
    """
    settings = RpcSettings(
        endpoints=[RpcEndpointConfig(name="declared", http_url="https://declared.example/rpc")]
    )

    endpoints = build_endpoints(settings, _PRIMARY, f"{_BACKUP_A},{_BACKUP_B}")

    assert [e.http_url for e in endpoints] == ["https://declared.example/rpc"]
