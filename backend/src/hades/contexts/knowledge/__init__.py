"""Knowledge — Hades' permanent, verifiable memory.

Every other context produces knowledge as a side effect of doing its job and
then, mostly, throws it away. The Scanner sees a token once. The Security Engine
reaches a verdict and moves on. The Committee predicts, and the prediction is
never compared with what actually happened. A paper trade closes and its result
— the single most expensive datum the platform can produce — reaches no ledger
at all. This context exists so none of that is lost again.

**What it is.** An append-only store of :class:`~.domain.models.Observation`
records, each one tagged with *where it came from* and *how strongly it is
verified*, plus the one piece of joining logic the platform was missing: the
:class:`~.application.journal.DecisionJournal`, which remembers the feature
vector as it stood **at the moment of the decision** and pairs it, later, with
the realised result of that decision. That pair is a
:class:`~.domain.models.Lesson` — a training sample with ground truth and no
temporal leakage.

**What it is not.** Knowledge takes no decision, sizes nothing and executes
nothing. It has no concept of an order, a position, a wallet balance or a
trading mode.

    It does not import ``execution``.
    It does not import ``portfolio``.
    It does not import ``risk``.
    It does not import ``learning``.

That is not a convention — ``tests/test_knowledge_isolation.py`` AST-parses this
package and fails the build on any of them. The restriction is what makes the
context safe to wire into everything: a memory that cannot act cannot be turned
into a back door.

**How it hears about the world.** It never subscribes to another context's event
classes. It consumes a narrow, self-owned inbound contract —
:class:`~.domain.models.KnowledgeEnvelope` — and the composition root
(``hades.ops.knowledge_runtime``) is what translates the platform's events into
it. That is a deliberate anti-corruption layer: upstream contexts can rename,
re-shape or delete their events without reaching into this one, and the
translation table is a single greppable file rather than a web of imports.

**Verification is a first-class property, not a comment.** A backtest result and
a settled paper trade are both knowledge, but they are not equally true. Every
record carries a :class:`~.domain.models.Verification` level, and consumers can —
and the training path does — demand ground truth rather than accepting a
simulation that happens to be shaped like one.
"""
