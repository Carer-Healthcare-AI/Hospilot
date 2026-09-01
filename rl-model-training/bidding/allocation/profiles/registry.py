"""Resource profiles — the multi-use-case seam.

Today there is one auction: a single ICU bed contested by ER, OT and Ward. There will be
others (OT slot, ventilator, dialysis chair, ambulance). A profile is everything that is
specific to *what is being auctioned*; the engine, ingest, budget, auction, policy and audit
layers know nothing about ICU beds.

Adding a second resource type should touch only:

* a new ``profiles/<resource>.py``
* a new ``config/caps_<resource>.yaml``, plus rows in ``config/rules/*.yaml`` keyed by
  resource type
* a new component module, but only if a genuinely new component is needed

If it touches anything else, the seam is in the wrong place — worth writing a throwaway
second profile during design purely to check.

**That check was run, and it failed twice.** ``profiles/hdu_bed.py`` was written as the
throwaway probe and found two leaks outside ``profiles/``: ``HospitalState`` was ICU-shaped,
so a second bed type had to report its beds in a field named ``icu_total_beds``; and
``load_config`` hardcoded one caps file while ``caps_config`` had no readers at all, so every
resource silently shared ICU's maxima. Both are fixed, and the claim above holds now. It did
not hold when it was first written — if you are adding a resource type, run the probe again
rather than trusting this paragraph.

**Caps and rule tables must not be shared across resource types.** The eight maxima were
chosen (never fitted) for an ICU bed. A ventilator auction reusing them would inherit a
calibration that was never valid for it. This is why :attr:`ResourceProfile.caps_config` has
no default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

from allocation.contracts import AgentKind, ComponentName, ResourceType

#: Anything that is not a letter or digit becomes a space, so ``ICU-bed``, ``icu_bed``,
#: ``I.C.U. bed`` and ``ICU bed`` all normalise to the same tokens. Without this the
#: matcher rejects a hyphen, which is not a distinction anyone typing a query intends.
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    return _NON_WORD.sub(" ", text.lower()).strip()


@dataclass(frozen=True, slots=True)
class UseCaseMatcher:
    """Which typed queries select this profile.

    Two token groups rather than whole phrases, because a phrase list only ever matches the
    word order someone happened to write down. *"one limited ICU bed"* and *"a bed in the
    ICU"* name the same resource; an exact-phrase list catches the first and silently
    rejects the second.

    ``qualifiers`` names the unit, ``nouns`` names the thing. A query matches when it
    contains at least one of each. That is deliberately permissive — the run echoes what it
    resolved to, and a wrong resolution is visible in the first line of the trace, whereas a
    rejection of a perfectly clear query is just an obstacle.

    It is *not* permissive across units: ``HDU bed`` carries no ICU qualifier, so it does not
    resolve to the ICU profile. Auctioning an ICU bed because someone asked about an HDU one
    would be a real allocation error, not a typo.

    **A unit word can name a bidder instead of a resource**, which is why :meth:`distance`
    exists. The founding use-case sentence is the example:

        *"ER, OT, and ICU/Ward demand compete for one limited ICU bed."*

    It contains ``ward`` and ``icu`` and one noun, so on presence alone it names two
    resources. But ``icu`` sits directly against ``bed`` while ``ward`` qualifies ``demand`` —
    a bidder, not the thing being auctioned. Token gap separates the two readings, and
    :func:`~allocation.trigger.query.resolve_profile` uses it only to break a tie between
    profiles that both matched, never to accept a query no profile matched.
    """

    qualifiers: tuple[str, ...]
    nouns: tuple[str, ...]

    def matched(self, query: str) -> tuple[str, ...]:
        """The qualifier and noun that matched, or ``()``."""
        text = f" {normalise(query)} "
        qualifier = next((q for q in self.qualifiers if f" {q} " in text), None)
        noun = next((n for n in self.nouns if f" {n} " in text), None)
        return (qualifier, noun) if qualifier and noun else ()

    def distance(self, query: str) -> int | None:
        """Fewest tokens between any qualifier and any noun, or ``None`` if either is absent.

        ``0`` means adjacent (*"ICU bed"*). Word order is irrelevant — *"a bed in the ICU"*
        measures the same gap either way, because the matcher must not privilege the order
        someone happened to write.
        """
        tokens = normalise(query).split()
        spans = [
            [
                (i, i + len(phrase))
                for phrase in (p.split() for p in group)
                for i in range(len(tokens) - len(phrase) + 1)
                if tokens[i : i + len(phrase)] == phrase
            ]
            for group in (self.qualifiers, self.nouns)
        ]
        qualifier_spans, noun_spans = spans
        if not qualifier_spans or not noun_spans:
            return None
        return min(
            # Non-overlapping spans: whichever starts later gives the gap between them.
            max(q_start - n_end, n_start - q_end, 0)
            for q_start, q_end in qualifier_spans
            for n_start, n_end in noun_spans
        )


@dataclass(frozen=True, slots=True)
class ResourceProfile:
    """Everything specific to one auctionable resource."""

    resource_type: ResourceType
    description: str
    matcher: UseCaseMatcher
    components: tuple[ComponentName, ...]
    eligible_agents: tuple[AgentKind, ...]
    ttl_minutes: Mapping[AgentKind, int]
    max_rounds: int
    round_seconds: int
    allocation_horizon_hours: float
    auction_key_bucket_minutes: int
    #: Which ``caps_<resource>.yaml`` this resource is scored against. **Deliberately has no
    #: default** — a profile that forgets to name its caps file must fail, not quietly inherit
    #: ICU's. Read by :meth:`allocation.config.Config.for_resource`.
    caps_config: str
    #: Which ``budget_<resource>.yaml`` funds this resource — **one pool per resource type**
    #: (D-3). Also has no default: budgets are denominated in utility points, and utility
    #: points from two caps tables are not the same unit. Read by ``Config.for_resource``.
    budget_config: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    def ttl_for(self, agent: AgentKind) -> int:
        try:
            return self.ttl_minutes[agent]
        except KeyError as exc:
            raise KeyError(
                f"no TTL configured for {agent.value} in profile {self.resource_type.value}"
            ) from exc

    def is_eligible(self, agent: AgentKind) -> bool:
        return agent in self.eligible_agents


class ProfileRegistry:
    """Resource type -> profile. One registration per resource, checked at import."""

    def __init__(self) -> None:
        self._profiles: dict[ResourceType, ResourceProfile] = {}

    def register(self, profile: ResourceProfile) -> ResourceProfile:
        if profile.resource_type in self._profiles:
            raise ValueError(f"profile for {profile.resource_type.value} already registered")
        self._profiles[profile.resource_type] = profile
        return profile

    def get(self, resource_type: ResourceType) -> ResourceProfile:
        try:
            return self._profiles[resource_type]
        except KeyError as exc:
            raise KeyError(
                f"no profile registered for {resource_type.value}; "
                f"registered: {[r.value for r in self._profiles]}"
            ) from exc

    def all(self) -> tuple[ResourceProfile, ...]:
        return tuple(self._profiles.values())


REGISTRY = ProfileRegistry()
