"""Knowledge's isolation is *structural* — this test is what makes it true.

The Knowledge context is wired into every producer on the platform. That is only
safe because it cannot act: it has no concept of an order, a position or a
trading mode, and no way to acquire one. The guarantee is worth exactly as much
as its enforcement, so it is enforced here, statically, and the build fails
before a violating import can ship.

Two things are checked, and the second matters as much as the first:

1. **The context imports nothing it must not.** Execution, risk, portfolio,
   learning — a memory that could reach the execution path would be a back door
   into it, however well-intentioned the caller.

2. **Its runtime does not reintroduce the coupling.** ``ops.knowledge_runtime``
   subscribes by event *name* rather than by class, precisely so the isolation
   is not undone by the wiring. The obvious cost of string subscriptions is that
   a renamed event stops being recorded in silence; that cost is paid here
   instead, by resolving every subscribed name against the real event classes. A
   rename breaks this test, not production.
"""

from __future__ import annotations

import ast
from pathlib import Path

import hades.contexts.knowledge as knowledge_pkg

#: A memory that can reach any of these is no longer only a memory.
_FORBIDDEN = (
    "hades.contexts.execution",
    "hades.contexts.risk",
    "hades.contexts.portfolio",
    # Learning is forbidden too, and for a subtler reason: the loop runs
    # Knowledge → Committee. If Knowledge could import learning, the temptation
    # would be to call the ledger directly and the dependency would become a
    # cycle. The composition root does the joining instead.
    "hades.contexts.learning",
)


def _module_files(package: object) -> list[Path]:
    root = Path(package.__file__).parent  # type: ignore[attr-defined]
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


def test_knowledge_never_imports_trading_or_learning_contexts() -> None:
    offenders: list[str] = []
    for path in _module_files(knowledge_pkg):
        for imported in _imports(path):
            if any(imported.startswith(prefix) for prefix in _FORBIDDEN):
                offenders.append(f"{path.name} imports {imported}")
    assert not offenders, "Knowledge must not depend on trading or learning contexts: " + "; ".join(
        offenders
    )


def test_knowledge_imports_no_other_bounded_context() -> None:
    """Stronger than the blocklist: Knowledge depends only on the shared kernel.

    Written as an allowlist because a blocklist only forbids the contexts we
    happened to think of. A new context added next year is covered by this and
    not by the one above.
    """
    offenders: list[str] = []
    for path in _module_files(knowledge_pkg):
        for imported in _imports(path):
            if imported.startswith("hades.contexts.") and not imported.startswith(
                "hades.contexts.knowledge"
            ):
                offenders.append(f"{path.name} imports {imported}")
    assert not offenders, "Knowledge may depend on the shared kernel only: " + "; ".join(offenders)


def test_knowledge_runtime_does_not_import_trading_contexts() -> None:
    """The wiring must not smuggle back what the context refuses."""
    import hades.ops.knowledge_runtime as runtime_mod

    path = Path(runtime_mod.__file__)
    offenders = [
        imported
        for imported in _imports(path)
        if any(imported.startswith(prefix) for prefix in _FORBIDDEN)
    ]
    assert not offenders, (
        "knowledge_runtime subscribes by event name so it need not import "
        "trading contexts: " + "; ".join(offenders)
    )


def test_every_subscribed_event_name_resolves_to_a_real_event() -> None:
    """String subscriptions must not rot silently.

    This is the guard that buys back the safety of not importing. Every name the
    runtime listens for is resolved against the platform's actual event classes;
    if an upstream context renames an event, the memory stops recording it — and
    this test says so at build time rather than leaving a producer quietly
    unheard in production.
    """
    from hades.bootstrap import _build_registry
    from hades.ops.knowledge_runtime import (
        EVT_COMMITTEE_PREDICTION,
        EVT_FEATURES_COMPUTED,
        EVT_POSITION_CLOSED,
        EVT_POSITION_OPENED,
        EVT_TRADE_APPROVED,
        KnowledgeRuntime,
    )

    # The registry is the platform's own list of every event that may cross the
    # transport boundary — the authoritative vocabulary to check against.
    registry = _build_registry()

    subscribed = set(KnowledgeRuntime._OBSERVED) | {
        EVT_FEATURES_COMPUTED,
        EVT_COMMITTEE_PREDICTION,
        EVT_TRADE_APPROVED,
        EVT_POSITION_OPENED,
        EVT_POSITION_CLOSED,
    }
    unknown = sorted(name for name in subscribed if registry.get(name) is None)
    assert not unknown, (
        "knowledge_runtime subscribes to event names that no longer exist "
        f"(renamed or removed upstream): {unknown}"
    )


def test_knowledge_has_no_vocabulary_for_acting() -> None:
    """A sanity check on the domain language itself.

    Isolation by import is necessary but not sufficient: a context could grow its
    own ``Order`` type and satisfy every import rule. The declared public surface
    is checked instead of ``dir()`` so the assertion tracks what the context
    *offers*, not what it happens to have imported.
    """
    from hades.contexts.knowledge.domain import models

    forbidden = ("order", "position", "execute", "signer", "keypair", "balance")
    surface = " ".join(models.__all__).lower()
    present = [word for word in forbidden if word in surface]
    assert not present, f"Knowledge's public domain vocabulary must not name actions: {present}"
