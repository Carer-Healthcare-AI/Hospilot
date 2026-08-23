"""Resolving the use-case sentence to a resource profile.

RL-Steps opens with a query, and that sentence is what selects *what is being auctioned*::

    "ER, OT, and ICU/Ward demand compete for one limited ICU bed. RL learns who should
     receive the bed while balancing survival, throughput, waiting time, cancellations,
     and financial impact."

**The query is the use case, not the trigger.** In production an auction opens on a
bed-release event, because three rounds of two minutes have to finish before a bed 30 minutes
out lands, and utilities carry 10-20 minute TTLs. A typed query fires the same
:func:`~allocation.trigger.runtime.run_allocation` path — one implementation, never two.

Two rules here exist to stop a convenience becoming a hazard:

**No default profile.** An unrecognised query raises. Silently falling back to the ICU-bed
profile would score a ventilator request against ICU-bed caps and produce a plausible,
entirely invalid number.

**A typed query is never LIVE.** :func:`manual_event` defaults to ``SIMULATION``, so a hand
fired run cannot hold a bed or move a real budget. Going live is an explicit argument.

When the token matcher is *inconclusive*, an optional LLM fallback
(:mod:`~allocation.trigger.llm_matcher`, opt-in via ``ALLOCATION_LLM_QUERY=1``) may resolve
the sentence semantically. Both rules above survive it: the model's answer is constrained to
the registered resource types plus "unknown", and "unknown" raises exactly like today.

Inconclusive means two things now that a bed *family* is registered rather than one bed:

* **Nothing matched.** *"Who should get the next available bed?"* names a bed but no unit.
* **Several matched equally.** A sentence can legitimately name two units — one as the
  resource, one as the bidder. Proximity resolves most of these (:meth:`UseCaseMatcher.
  distance`); what proximity cannot separate goes to the model, which may only choose among
  the profiles the tokens already found.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Sequence

from allocation.contracts import AuctionMode, ReleaseEvent, TriggerSource
from allocation.profiles.registry import REGISTRY, ResourceProfile
from allocation.trigger import llm_matcher


class UnknownUseCase(LookupError):
    """The query does not name a resource any registered profile auctions."""


def match_profiles(query: str) -> tuple[tuple[ResourceProfile, tuple[str, ...]], ...]:
    """Every profile the query names, with the tokens that matched it.

    Matching lives on the profile (:class:`~allocation.profiles.registry.UseCaseMatcher`),
    not here. Adding a resource type should touch one file in ``profiles/``; a phrase table
    in this module would make it two, and the second would be the one people forget.

    Pairs rather than a mapping: a profile carries a ``ttl_minutes`` dict and so is not
    hashable.
    """
    return tuple(
        (profile, matched)
        for profile in REGISTRY.all()
        if (matched := profile.matcher.matched(query))
    )


def resolve_profile(query: str) -> ResourceProfile:
    """The profile a query selects.

    Raises on no match and on more than one — an ambiguous query is a request the caller has
    to disambiguate, not one to resolve by picking the first hit.
    """
    if not query or not query.strip():
        raise UnknownUseCase("empty query; nothing selects a resource profile")

    hits = match_profiles(query)

    if not hits:
        semantic = llm_matcher.resolve(query)
        if semantic is not None:
            return REGISTRY.get(semantic)
        known = sorted(
            f"{q} + {n}"
            for profile in REGISTRY.all()
            for q in profile.matcher.qualifiers[:1]
            for n in profile.matcher.nouns[:1]
        )
        hint = (
            ""
            if llm_matcher.enabled()
            else " Set ALLOCATION_LLM_QUERY=1 to let an LLM resolve free-form phrasings."
        )
        raise UnknownUseCase(
            f"no registered resource matches {query.strip()[:80]!r}. A query needs a word "
            f"naming the unit and a word naming the thing — e.g. {known}. There is "
            "deliberately no default: caps are per-resource, so scoring one resource with "
            f"another's caps yields a plausible number that is valid for nothing.{hint}"
        )

    if len(hits) > 1:
        return _closest(query, hits)

    return hits[0][0]


def _closest(
    query: str, hits: tuple[tuple[ResourceProfile, tuple[str, ...]], ...]
) -> ResourceProfile:
    """The profile whose unit word sits nearest the noun, when several matched.

    A unit word can name a *bidder* rather than the resource. The founding use-case sentence
    does exactly that — *"ER, OT, and ICU/Ward demand compete for one limited ICU bed"* names
    ICU and Ward, but ``ward`` qualifies ``demand`` while ``icu`` sits against ``bed``.
    Refusing that sentence would refuse the query this engine was built to answer.

    Only a *strict* winner resolves on tokens alone. A genuine two-resource query — *"we have
    an ICU bed and a ward bed free"* — ties, and a tie hands the sentence to the LLM fallback,
    which reads it semantically instead of by proximity.

    **The fallback may only pick from the profiles the tokens already found.** Tokens
    established which readings are plausible; the model is disambiguating between them, not
    overriding them. An answer outside that set, or ``"unknown"``, raises — as does every tie
    when the fallback is disabled, which is the default and the state every test runs in.
    """
    scored = [(p.matcher.distance(query), p, t) for p, t in hits]
    measurable = [(d, p, t) for d, p, t in scored if d is not None]
    best = min((d for d, _p, _t in measurable), default=None)
    winners = [(p, t) for d, p, t in measurable if d == best]

    if len(winners) == 1:
        return winners[0][0]

    semantic = llm_matcher.resolve(query)
    if semantic is not None:
        chosen = next((p for p, _t in hits if p.resource_type is semantic), None)
        if chosen is not None:
            return chosen

    named = {p.resource_type.value: t for p, t in hits}
    raise UnknownUseCase(
        f"query names more than one resource, none of them more closely than the others: "
        f"{named}. Name the unit, or run one auction per resource."
    )


def matched_tokens(query: str, profile: ResourceProfile) -> str:
    """How the query resolved, for the trace.

    Shown on every run because the matcher is permissive by design: a wrong resolution has to
    be visible in the first line of output, not discovered from the utilities afterwards. An
    LLM resolution says so explicitly — it must not masquerade as a token match.

    The memo is checked *before* the tokens, because a tie broken by the LLM has both: the
    profile's own tokens matched, and so did another profile's. Reporting "hdu + bed" there
    would hide that the sentence was ambiguous and a model chose.
    """
    tokens = profile.matcher.matched(query)
    if llm_matcher.cached(query) is profile.resource_type:
        if tokens:
            others = [p.resource_type.value for p, _t in match_profiles(query)]
            return f"{' + '.join(tokens)} (ambiguous with {others}; llm chose this one)"
        return "(no token matched — resolved semantically by llm)"
    if tokens:
        return " + ".join(tokens)
    return "(profile supplied directly)"


def manual_event(
    profile: ResourceProfile,
    now: datetime,
    resource_id: str | None = None,
    lead_minutes: float = 30.0,
    mode: AuctionMode = AuctionMode.SIMULATION,
) -> ReleaseEvent:
    """A :class:`ReleaseEvent` for a hand-fired run.

    ``lead_minutes`` is RL-Steps section 1's *"one ICU bed expected to become available in 30
    minutes"*. ``mode`` defaults to ``SIMULATION``: a typed query must not be able to hold a
    bed, and the mode is on the auction row so a test run is never mistaken for a real
    allocation afterwards.

    ``resource_id`` defaults to the profile's own placeholder rather than a literal. It used to
    read ``"icu-bed-manual"`` for every resource, so a ward-bed auction persisted an audit row
    naming an ICU bed — the same ICU-shaped default the unit-scoped read removed elsewhere,
    landing in the one column that identifies *which* bed was at stake.
    """
    return ReleaseEvent(
        event_id=str(uuid.uuid4()),
        resource_type=profile.resource_type,
        resource_id=resource_id or f"{profile.resource_type.value}-manual",
        predicted_free_at=now + timedelta(minutes=lead_minutes),
        detected_at=now,
        source=TriggerSource.MANUAL_QUERY,
        mode=mode,
    )


def describe(profile: ResourceProfile) -> Sequence[tuple[str, str]]:
    """Rows for the trace: what this profile commits the run to."""
    return (
        ("resource", profile.resource_type.value),
        ("bidders", ", ".join(a.value for a in profile.eligible_agents)),
        ("components", str(len(profile.components))),
        # A ceiling, not a schedule: the lead time and the tightest TTL can cut it, and
        # quiescence can end the auction earlier still. The close step reports what ran.
        ("rounds", f"up to {profile.max_rounds} x {profile.round_seconds}s"),
        ("ttl (min)", ", ".join(f"{a.value} {t}" for a, t in profile.ttl_minutes.items())),
        ("horizon", f"{profile.allocation_horizon_hours:g} h"),
    )
