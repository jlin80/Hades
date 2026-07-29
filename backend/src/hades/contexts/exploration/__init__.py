"""Exploration — the deliberate, budgeted answer to the cold start.

Every other context on this platform exists to make money or to keep it. This
one does not. Its purpose is to buy **evidence**, at a price fixed in advance,
during the only period in which the platform has none.

The defect it addresses is structural rather than numeric. The AI Committee is
validated against a ledger of settled trades; the ledger only fills when trades
settle; trades only happen when the committee is confident enough to clear the
Risk Manager's conviction gates. With an empty memory the committee is confident
about nothing, so nothing trades, so the memory stays empty. Lowering the gates
would break the deadlock the wrong way: it would lower them for *all* capital,
permanently, on the strength of no evidence at all — which is precisely the
decision the evidence was meant to inform.

Exploration breaks it the other way. A trade that the conviction gates muted may
still be taken, but only:

* while the memory demonstrably lacks the evidence to decide (and never after);
* at a size fixed in configuration, not derived from conviction;
* against an **independent budget** with daily, weekly and lifetime ceilings;
* after passing **every safety rule unchanged** — security, developer, wallet,
  liquidity, kill switch, circuit breaker, drawdown, exposure, capital.

Four properties are load-bearing and each is fixed by a test:

1. **It never authorises anything.** This context produces a *verdict on
   eligibility*, not a decision. The Risk Manager remains the only component that
   can approve a trade, and an exploration grant is one more input to its chain,
   never a bypass of it. Nothing here can construct a ``TradeApproved``.
2. **It waives conviction, never safety.** The Risk Manager splits its quality
   rules into two tuples for this reason: a rule in the safety tuple cannot be
   waived by an exploration grant even by mistake, because the manager only ever
   consults the conviction tuple when deciding what a grant covers.
3. **It turns itself off.** Sufficiency is a stated arithmetic condition —
   ``lessons >= N`` with at least ``k`` of each class — evaluated on every
   request. When it is met the context latches off and says so on the bus. There
   is no operator action required for exploration to end, and no configuration in
   which it runs forever.
4. **It is explainable end to end.** Every verdict carries the arithmetic behind
   it: which condition was checked, what the numbers were, and what tipped it.
   There is no model here, no exploration/exploitation heuristic, no randomness.
   Candidate selection is a deterministic under-sampled-cohort rule that a person
   can recompute by hand from the same numbers.

The context imports nothing but the shared kernel (and, at its infrastructure
edge only, Knowledge's *domain* — the read side of the memory whose emptiness is
its whole justification). ``tests/test_exploration_isolation.py`` enforces that
as an allowlist.
"""

from __future__ import annotations
