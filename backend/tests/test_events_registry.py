"""The event registry round-trips events across the transport boundary."""

from __future__ import annotations

from hades.contexts.notification.domain.events import NotificationRequested
from hades.contexts.notification.domain.ports import Severity
from hades.shared_kernel.domain.identifiers import new_id
from hades.shared_kernel.events import EventRegistry


def test_registry_rebuilds_typed_event_from_envelope() -> None:
    registry = EventRegistry()
    registry.register(NotificationRequested)

    original = NotificationRequested(
        aggregate_id=new_id(),
        title="t",
        body="b",
        severity=Severity.CRITICAL,
        tags={"topic": "risk"},
    )
    envelope = original.to_envelope()

    rebuilt = registry.rebuild(envelope)
    assert isinstance(rebuilt, NotificationRequested)
    assert rebuilt.title == "t"
    assert rebuilt.severity is Severity.CRITICAL
    assert rebuilt.tags == {"topic": "risk"}


def test_registry_ignores_unknown_event() -> None:
    registry = EventRegistry()
    assert registry.rebuild({"event_type": "Nope", "payload": {}}) is None


def test_no_two_registered_events_share_a_routing_key() -> None:
    """The bus routes on the class name, so two events may not share one.

    This is a real defect the platform carried for a long time, found while
    wiring the Knowledge memory in Phase 2: ``contexts/research`` and
    ``contexts/strategy`` both defined a ``StrategyPromoted``. They collided on
    one key, so ``EventRegistry`` kept whichever was registered last. Under the
    Redis transport a *research* promotion — the most governance-sensitive event
    the lab emits — was rebuilt as a strategy-engine promotion with a different
    payload schema, and the audit trail labelled it as one.

    Nothing raised. The registry's ``dict`` accepted the second registration
    silently, which is exactly why this needs a test rather than care.

    The scan walks every ``domain/events.py`` in the codebase rather than only
    what ``bootstrap`` registers, so the collision is caught when the class is
    written, not when somebody remembers to register it.
    """
    import ast
    from collections import defaultdict
    from pathlib import Path

    import hades.contexts as contexts_pkg

    root = Path(contexts_pkg.__file__).parent
    owners: dict[str, list[str]] = defaultdict(list)
    for path in sorted(root.rglob("domain/events.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        context = path.relative_to(root).parts[0]
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(
                isinstance(base, ast.Name) and base.id == "DomainEvent" for base in node.bases
            ):
                owners[node.name].append(context)

    collisions = {name: sorted(where) for name, where in owners.items() if len(where) > 1}
    assert not collisions, (
        "Two contexts define an event with the same class name; the bus routes on "
        f"that name so one would shadow the other: {collisions}"
    )
