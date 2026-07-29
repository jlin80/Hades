"""Narrative classification — which meme story a token is telling.

Meme coins do not trade on fundamentals; they trade on a *narrative*, and the
narrative is the strongest cohort a candidate belongs to. Two dog coins launched
an hour apart on the same venue behave far more like each other than like the
politics coin that launched between them, so "how did tokens telling this story
work out?" is one of the few questions the memory can answer usefully while it
is still small.

The classifier is deliberately a **transparent keyword map**, not a model:

* it must be *stable over years* — a narrative label that drifts silently would
  quietly repartition every historical cohort, and the memory would compare
  today's candidates against a differently-defined past;
* it has to run on the hot path with no dependencies;
* and it must be explainable, because the label ends up in the audit trail of a
  decision.

An unrecognised token gets ``None``, never a guess. No cohort is better than a
wrong cohort: an unknown narrative simply means that one dimension of the
enrichment stays silent, while a mislabelled one pollutes a cohort permanently.
"""

from __future__ import annotations

import re
from typing import Final

#: narrative → the tokens (substrings) that identify it. Ordered by specificity:
#: the first family with a hit wins, so "pepe" is not swallowed by "frog".
_FAMILIES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("pepe", ("pepe", "peepee", "pepa")),
    ("doge", ("doge", "dog", "shib", "inu", "bonk", "wif", "puppy", "corgi", "husky")),
    ("cat", ("cat", "kitty", "meow", "popcat", "mew")),
    ("frog", ("frog", "toad", "ribbit")),
    ("ai", ("ai", "gpt", "agent", "llm", "neural", "robot", "bot", "singularity")),
    ("politics", ("trump", "biden", "maga", "potus", "election", "president", "vote")),
    ("animal", ("bear", "bull", "monkey", "ape", "penguin", "whale", "shark", "hippo")),
    ("food", ("pizza", "burger", "taco", "coffee", "banana", "cheese", "donut")),
    ("space", ("moon", "mars", "rocket", "space", "star", "galaxy", "cosmic")),
    ("finance", ("usd", "gold", "bank", "yield", "bond", "dollar", "fed")),
    ("culture", ("chad", "wojak", "based", "gigachad", "sigma", "npc", "meme")),
)

#: Split on anything that is not a letter or digit, so "BABY-DOGE_2" yields
#: ("baby", "doge", "2") and a substring match cannot straddle a separator.
_SPLIT: Final = re.compile(r"[^a-z0-9]+")


def narrative_of(*sources: str | None) -> str | None:
    """Classify a token's narrative from its name, symbol and description.

    Matching is done on whole words first and only then on substrings, because
    a substring rule alone makes "CATALYST" a cat coin. Returns ``None`` when
    nothing matches.
    """
    text = " ".join(s.lower() for s in sources if s)
    if not text.strip():
        return None
    words = {w for w in _SPLIT.split(text) if w}

    for narrative, keywords in _FAMILIES:
        if any(keyword in words for keyword in keywords):
            return narrative
    # Substring pass, restricted to keywords long enough that an accidental hit
    # is unlikely ("ai" inside "chain" is exactly the failure this avoids).
    for narrative, keywords in _FAMILIES:
        for keyword in keywords:
            if len(keyword) >= 4 and keyword in text:
                return narrative
    return None


__all__ = ["narrative_of"]
