"""The Research Lab's isolation is *structural* — this test enforces it.

The whole safety argument rests on one fact: the lab has no path to execution.
We prove it by statically scanning every module in ``contexts/research`` and
asserting none imports the Execution, Risk or Portfolio contexts. If someone ever
wires such a dependency, this test fails before the code can ship — the lab must
never be able to place an order or enable live trading.
"""

from __future__ import annotations

import ast
from pathlib import Path

import hades.contexts.research as research_pkg

_FORBIDDEN = (
    "hades.contexts.execution",
    "hades.contexts.risk",
    "hades.contexts.portfolio",
)


def _module_files() -> list[Path]:
    root = Path(research_pkg.__file__).parent
    return sorted(root.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_research_context_never_imports_execution_risk_or_portfolio() -> None:
    offenders: list[str] = []
    for path in _module_files():
        for imported in _imports(path):
            if any(imported.startswith(f) for f in _FORBIDDEN):
                offenders.append(f"{path.name} imports {imported}")
    assert not offenders, "Research Lab must not depend on trading contexts: " + "; ".join(
        offenders
    )


def test_research_does_not_import_knowledge_either() -> None:
    """The lab feeds permanent memory, and must not know that it does.

    Research became the platform's official knowledge producer in Phase 2, and
    the obvious way to build that would have been to hand the lab a recorder to
    call. The connection is domain events instead: the lab publishes what it
    finished, the memory happens to be listening, and either could be deleted
    without the other failing to import.

    That is not stylistic. A direct call would put an ingestion failure on the
    lab's critical path — a memory outage would start failing research runs — and
    would give a context that must never act a handle on a collaborator that
    writes. Events keep the coupling to a name on a bus.
    """
    offenders: list[str] = []
    for path in _module_files():
        for imported in _imports(path):
            if imported.startswith("hades.contexts.knowledge"):
                offenders.append(f"{path.name} imports {imported}")
    assert not offenders, "Research must reach Knowledge only through domain events: " + "; ".join(
        offenders
    )


def test_knowledge_does_not_import_research_either() -> None:
    """And symmetrically. The memory subscribes to event *names*; it has no
    compile-time knowledge that a Research Lab exists at all."""
    import hades.contexts.knowledge as knowledge_pkg

    root = Path(knowledge_pkg.__file__).parent
    offenders = [
        f"{path.name} imports {imported}"
        for path in sorted(root.rglob("*.py"))
        for imported in _imports(path)
        if imported.startswith("hades.contexts.research")
    ]
    assert not offenders, "Knowledge must not depend on Research: " + "; ".join(offenders)


def test_research_domain_events_are_facts_not_instructions() -> None:
    """Sanity: the promotion event carries a decision, never an order payload."""
    from hades.contexts.research.domain.events import ResearchStrategyPromoted
    from hades.contexts.research.domain.models import (
        CandidateKind,
        PromotionDecision,
        PromotionOutcome,
        ValidationStage,
    )
    from hades.shared_kernel.domain.identifiers import new_id

    decision = PromotionDecision(
        candidate_id=new_id(),
        candidate_name="x",
        kind=CandidateKind.STRATEGY,
        outcome=PromotionOutcome.APPROVED,
        stage=ValidationStage.SHADOW,
        manual_approved=True,
    )
    event = ResearchStrategyPromoted(aggregate_id=new_id(), decision=decision)
    payload = event.to_envelope()["payload"]
    # No order/size/mode fields — it is a governance fact only.
    assert "order" not in payload
    assert "size_usd" not in payload
    assert payload["decision"]["manual_approved"] is True
