"""Exploration's isolation is *structural* — this test is what makes it true.

This context exists to argue for trades the Risk Manager would otherwise refuse.
That is an unusual thing to have on the platform, and it is only safe because
exploration cannot act on its own argument: it holds no executor, no portfolio,
no wallet and no risk manager, and it has no way to acquire one. The guarantee is
worth exactly as much as its enforcement, so it is enforced statically and the
build fails before a violating import can ship.

Four things are checked, and the third is the one that would actually bite:

1. **It never imports a trading context.** Execution, risk, portfolio. A
   component that could reach the execution path would be a back door into it,
   and this one is *designed* to want more trades than the guardian allows.
2. **Its dependencies are an allowlist, not a blocklist.** Shared kernel, plus
   Knowledge's domain at the infrastructure edge only. A blocklist forbids the
   contexts we happened to think of; an allowlist covers the context somebody
   adds next year without them having to remember this file exists.
3. **The Risk Manager's safety rules are not in the waivable tuple.** The whole
   design rests on that split, and it is a two-line edit away from being wrong —
   moving ``SecurityPolicy`` into the conviction tuple would compile, pass every
   other test, and let exploration buy rug pulls a dollar at a time.
4. **Its events are registered on the bus.** Under the Redis transport an
   unregistered event is silently dropped at the process boundary, which is
   exactly how three ``Order*`` events went unheard for months.
"""

from __future__ import annotations

import ast
from pathlib import Path

import hades.contexts.exploration as exploration_pkg

#: A programme that can reach any of these is no longer only a programme.
_FORBIDDEN = (
    "hades.contexts.execution",
    "hades.contexts.risk",
    "hades.contexts.portfolio",
    # Learning is forbidden for the same reason Knowledge forbids it: the
    # conviction gates exploration waives are computed there, and a context that
    # could reach the committee would be a context that could argue with it.
    "hades.contexts.learning",
    "hades.contexts.strategy",
)

#: Everything exploration may depend on. Knowledge's *domain* only, and only
#: because the emptiness of that memory is the entire justification for this
#: context existing — it has to be able to ask.
_ALLOWED_CONTEXTS = ("hades.contexts.exploration", "hades.contexts.knowledge.domain")


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


def test_exploration_never_imports_a_trading_or_learning_context() -> None:
    offenders: list[str] = []
    for path in _module_files(exploration_pkg):
        for imported in _imports(path):
            if any(imported.startswith(prefix) for prefix in _FORBIDDEN):
                offenders.append(f"{path.name} imports {imported}")
    assert not offenders, (
        "Exploration argues for trades and must not be able to take one: " + "; ".join(offenders)
    )


def test_exploration_depends_only_on_the_shared_kernel_and_knowledges_domain() -> None:
    offenders: list[str] = []
    for path in _module_files(exploration_pkg):
        for imported in _imports(path):
            if imported.startswith("hades.contexts.") and not imported.startswith(
                _ALLOWED_CONTEXTS
            ):
                offenders.append(f"{path.name} imports {imported}")
    assert not offenders, (
        "Exploration may depend on the shared kernel and Knowledge's domain only: "
        + "; ".join(offenders)
    )


def test_the_memory_is_only_reached_from_the_infrastructure_edge() -> None:
    """Domain and application stay pure; only an adapter knows Knowledge exists.

    Same shape as the AI Committee's history adapter. It keeps the policy — the
    part that decides how public money is spent — testable with a literal census
    and no store anywhere near it.
    """
    root = Path(exploration_pkg.__file__).parent  # type: ignore[attr-defined]
    offenders = [
        f"{path.relative_to(root)} imports {imported}"
        for path in _module_files(exploration_pkg)
        if path.parent.name != "infrastructure"
        for imported in _imports(path)
        if imported.startswith("hades.contexts.knowledge")
    ]
    assert not offenders, "Knowledge is reachable only from Exploration's edge: " + "; ".join(
        offenders
    )


def test_exploration_has_no_vocabulary_for_acting() -> None:
    """Isolation by import is necessary but not sufficient: a context could grow
    its own ``Order`` type and satisfy every import rule."""
    from hades.contexts.exploration.domain import models

    forbidden = ("order", "position", "execute", "signer", "keypair", "wallet", "fill")
    surface = " ".join(models.__all__).lower()
    present = [word for word in forbidden if word in surface]
    assert not present, f"Exploration's public vocabulary must not name actions: {present}"


def test_a_grant_cannot_express_an_approval() -> None:
    """The strongest statement the port can carry is a ceiling, not a decision.

    If :class:`ExplorationGrant` ever grew an ``approved`` flag, the Risk
    Manager would stop being the only authoriser — and the change would look,
    in review, like one more field on a value object.
    """
    from hades.contexts.risk.domain.models import ExplorationGrant

    fields = set(ExplorationGrant.model_fields)
    assert not fields & {"approved", "decision", "authorise", "authorize", "execute"}
    assert "notional_usd" in fields and "waived_policy" in fields


def test_safety_policies_are_not_in_the_waivable_tuple() -> None:
    """The load-bearing split, asserted by name.

    An exploration grant waives exactly one policy from the *conviction* tuple.
    Which rules live in which tuple is therefore the security boundary of the
    whole programme, and it is decided in one composition function — so it is
    checked here rather than left to whoever next edits that function.
    """
    from hades.contexts.risk.application.factory import build_risk_manager
    from hades.contexts.risk.domain.models import RiskConfig

    manager = build_risk_manager(RiskConfig())
    safety = {policy.name for policy in manager._quality}
    conviction = {policy.name for policy in manager._conviction}

    assert {"security", "developer", "wallet", "liquidity"} <= safety
    assert conviction == {"min_probability", "min_confidence"}
    assert not safety & conviction


def test_exploration_events_are_registered_on_the_bus() -> None:
    """An unregistered event is discarded at the Redis process boundary, and
    silently: ``EventRegistry.rebuild`` returns ``None`` and the bus drops it.
    That is how the Execution Engine's fills went unheard for months."""
    from hades.bootstrap import _build_registry
    from hades.contexts.exploration.domain import events as exploration_events

    registry = _build_registry()
    unknown = sorted(
        name for name in exploration_events.__all__ if registry.get(name) is None
    )
    assert not unknown, f"exploration events missing from the event registry: {unknown}"


def test_knowledge_records_the_exploration_programme() -> None:
    """Permanent memory must hear the programme observing itself.

    Without these rows the platform would hold the trades but not the fact that
    a budget bought them, and no later analysis could separate what it learned
    from what it believed.
    """
    from hades.ops.knowledge_runtime import KnowledgeRuntime

    observed = set(KnowledgeRuntime._OBSERVED)
    assert {
        "ExplorationGranted",
        "ExplorationSpent",
        "ExplorationBudgetExhausted",
        "ExplorationCompleted",
    } <= observed


def test_an_external_bundle_cannot_claim_to_be_the_exploration_programme() -> None:
    """The import allowlist is a whitelist, so a new platform source is excluded
    by default — but that is worth asserting rather than assuming, because the
    consequence of getting it wrong is a file on disk posing as settled spend."""
    from hades.contexts.knowledge.domain.bundles import _EXTERNAL_SOURCES
    from hades.contexts.knowledge.domain.models import KnowledgeSource

    assert KnowledgeSource.EXPLORATION not in _EXTERNAL_SOURCES
