"""Ports defined by the Execution Engine — the paper/live seam and its adapters.

The Execution Engine depends only on these abstractions; the composition root
binds concrete adapters. The most important is :class:`Executor`: the single
interface behind which ``PaperExecutor`` and ``LiveExecutor`` sit. The rest let
the engine price a fill, quote a swap, sign/confirm a live transaction and
persist the order/transaction trail without knowing the concrete technology.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from hades.contexts.common.domain.value_objects import TokenRef
from hades.contexts.execution.domain.models import (
    FillReport,
    OrderRequest,
    Quote,
    SendReceipt,
)


@runtime_checkable
class Executor(Protocol):
    """Executes an order. THE single interface behind which paper and live sit.

    ``PaperExecutor`` and ``LiveExecutor`` both satisfy this. Because the return
    type (:class:`FillReport`) is identical, everything downstream (Portfolio,
    Learning, Dashboard) is oblivious to which mode produced a fill.
    """

    @property
    def mode(self) -> str:
        """ "paper" or "live" — for audit/telemetry, not for caller branching."""
        ...

    async def execute(self, request: OrderRequest) -> FillReport: ...


@runtime_checkable
class TransactionSigner(Protocol):
    """Signs Solana transactions (live only). Keys never leave the adapter.

    Implementations read the keypair from a mounted secret; the platform code
    never touches raw key material (see the security rules in ``.env.example``).
    """

    @property
    def public_key(self) -> str: ...

    async def sign_and_send(self, serialized_tx: bytes) -> str: ...


@runtime_checkable
class TransactionSubmitter(Protocol):
    """Carries an already-built transaction to the network — the *transport* seam.

    :class:`TransactionSigner` conflates signing and sending in one call, which is
    correct for the plain RPC path but leaves no place to plug a different
    transport. This port isolates the part that actually differs between a plain
    RPC send, a staked/dual-routed sender and a Jito bundle: where the bytes go,
    what it costs in tip, and how long it takes.

    Implementations must be **fail-closed**: a submission that could not be
    handed to the network returns ``accepted=False`` with a reason, never an
    optimistic signature. See ``docs/EXECUTION_FAST_PATH_2026-08-04.md`` for the
    routes this is meant to accommodate.
    """

    @property
    def route(self) -> str:
        """Stable label for the transport ("signer", "sender", "jito"…)."""
        ...

    async def submit(self, serialized_tx: bytes) -> SendReceipt: ...


@runtime_checkable
class PriceOracle(Protocol):
    """Returns the current USD price of a token, or ``None`` when unknown.

    The :class:`PaperExecutor` uses this to turn a USD notional into a token
    quantity so the simulation is grounded in a real price. When no price is
    available the executor degrades to a unit-price simulation (quantity == USD)
    rather than inventing a number.
    """

    async def price_usd(self, token: TokenRef) -> Decimal | None: ...


@runtime_checkable
class QuoteProvider(Protocol):
    """Quotes (and builds) a swap on a DEX/aggregator — live only.

    ``quote`` returns the expected amounts and price impact; ``build_swap_tx``
    turns an accepted quote into a serialized, unsigned transaction ready for the
    :class:`TransactionSigner`. Implementations talk to Jupiter/Raydium/etc.
    """

    async def quote(
        self, *, input_mint: str, output_mint: str, amount: Decimal, slippage_bps: int
    ) -> Quote: ...

    async def build_swap_tx(self, quote: Quote, *, owner: str) -> bytes: ...


@runtime_checkable
class RpcGateway(Protocol):
    """The subset of the RPC Manager the live executor + confirmation need.

    Depending on the concrete :class:`~hades.shared_kernel.solana.rpc_manager.RpcManager`
    directly would couple execution to infrastructure; this port keeps the seam
    clean and trivially fakeable in tests.
    """

    async def call(self, method: str, params: list[Any] | None = None) -> Any: ...

    @property
    def active_endpoint(self) -> str | None: ...


@runtime_checkable
class ProductionChecklistPort(Protocol):
    """Extra live-readiness checks contributed by the platform (Phase 10).

    The trading-mode switch already runs a readiness gate before enabling live.
    This structural port lets a checklist (implemented in the monitoring context)
    add its own required checks — a failing infra/subsystem or an active Emergency
    Mode then blocks the switch to LIVE. Returned as primitive tuples
    ``(name, ok, detail, required)`` so execution needs no monitoring import.
    """

    async def readiness(self) -> list[tuple[str, bool, str, bool]]: ...


@runtime_checkable
class OrderStore(Protocol):
    """Durable, best-effort persistence of the order ledger (mode-agnostic)."""

    async def save(self, snapshot: dict[str, Any]) -> None: ...

    async def recent(self, *, limit: int = 50) -> list[dict[str, Any]]: ...


@runtime_checkable
class TransactionStore(Protocol):
    """Durable, best-effort persistence of transaction detail (fees, signature…)."""

    async def record(self, detail: dict[str, Any]) -> None: ...

    async def recent(self, *, limit: int = 50) -> list[dict[str, Any]]: ...
