"""Transaction submitters — the transports a built transaction can travel over.

Only one exists today: :class:`SignerSubmitter`, which is the current behaviour
expressed through the new :class:`~...domain.ports.TransactionSubmitter` port. It
adds no capability; it exists so the *fast path* has a seam to swap, and so the
plain RPC route is a measurable baseline rather than an unlabelled default.

The routes the port was shaped for — a staked/dual-routed sender, and Jito
bundles with a dynamic tip — are **not implemented here**. Both cost money per
transaction, and per the operating rules that is not a decision this code makes.
The options, their prices and the arithmetic that should decide between them are
in ``docs/EXECUTION_FAST_PATH_2026-08-04.md``.
"""

from __future__ import annotations

from hades.contexts.execution.domain.models import SendReceipt
from hades.contexts.execution.domain.ports import TransactionSigner
from hades.shared_kernel.logging import get_logger

_logger = get_logger("execution.submitter")

#: Route label for the plain sign-and-send path. The comparison baseline.
ROUTE_SIGNER = "signer"


class SignerSubmitter:
    """Submits via the :class:`TransactionSigner`'s own send. Satisfies the port.

    This is the baseline route: no tip, no auction, no staked lane — the
    transaction competes in the public path. Any future route has to beat *this*
    on measured landing latency to justify what it charges.
    """

    def __init__(self, signer: TransactionSigner) -> None:
        self._signer = signer

    @property
    def route(self) -> str:
        return ROUTE_SIGNER

    async def submit(self, serialized_tx: bytes) -> SendReceipt:
        try:
            signature = await self._signer.sign_and_send(serialized_tx)
        except Exception as exc:
            # Fail closed: the caller must never read this as a maybe-sent.
            _logger.warning("submit_failed", route=self.route, error=str(exc))
            return SendReceipt(accepted=False, route=self.route, error=str(exc))
        return SendReceipt(signature=signature, accepted=True, route=self.route)
