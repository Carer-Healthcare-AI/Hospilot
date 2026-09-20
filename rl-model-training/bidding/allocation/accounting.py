"""Lifecycle and settlement accounting — resource-neutral, statistics only.

**No domain model here.** This module never learns what a bed, a scanner or a ventilator is; it
imports nothing from :mod:`allocation.use_cases`. What it shares is two identities every family
must satisfy and the arithmetic that checks them.

---------------------------------------------------------------------------------------
1. The lifecycle identity
---------------------------------------------------------------------------------------

::

    arrivals = served + diverted + expired + abandoned + censored

Every request that arrives must reach exactly one final disposition, and censoring is a
disposition rather than an absence: a request still live when the horizon closed did not succeed
and did not fail, and dropping it takes it out of the denominator of every rate. A simulator that
loses a request reports a flattering timeliness that nothing in its own output contradicts.

Three ways it can fail, and all three are checked rather than assumed:

* a request **disappears** — arrived, never resolved;
* a request is **resolved twice**, or resolved with two incompatible fates;
* a request is resolved that **never arrived**.

The five dispositions are deliberately coarse and resource-neutral. Each family supplies its own
``fate -> Disposition`` map, and an unmapped fate is a *failure*, not a default: silently binning
an unrecognised fate into ``CENSORED`` is how a relabelled abandonment slips past a safety gate.

**A policy must not pass by relabelling.** Abandonment, expiry and censoring are three different
names for "this patient did not get the resource", so a gate written on one of them alone can be
driven to zero by renaming. :attr:`LifecycleAudit.unresolved` sums all three.

---------------------------------------------------------------------------------------
2. The settlement identity
---------------------------------------------------------------------------------------

Every participant with a standing bid is charged **exactly once**, at settlement, and a
no-award event charges no winner. :func:`settlement_problems` checks a closed event against the
charge rows it produced without knowing what was being auctioned.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum


class Disposition(str, Enum):  # noqa: UP042 - see the note below
    """The five mutually exclusive ways a request's life can end.

    A ``str`` enum rather than ``StrEnum``, deliberately and consistently with every other enum
    in this codebase (``AgentKind``, ``QAction``, ``EquipmentStatus``, ``Standing``, ...). These
    values are serialised into audit rows and read back years later, and ``str`` subclassing is
    what lets an old JSON string compare equal to the member without a conversion step at every
    boundary. Switching one enum to ``StrEnum`` would make this the only one with different
    comparison semantics.

    Coarse on purpose. A family's own fate vocabulary is richer — critical care separates
    ``assigned_timely`` from ``assigned_late``, diagnostic ``answered`` from ``answered_late`` —
    and those distinctions belong to the family. What has to be shared is the partition, because
    that is what the identity is stated over.
    """

    #: Received the resource. Timely or late — lateness is a harm, not a lost request, and is
    #: reported separately so it cannot hide inside "served".
    SERVED = "served"
    #: An alternative pathway was arranged and activated. Did not consume the scarce resource.
    DIVERTED = "diverted"
    #: The clinically useful window elapsed with nothing arranged.
    EXPIRED = "expired"
    #: Left with nothing arranged, by choice of the policy.
    ABANDONED = "abandoned"
    #: Still live, or never reached, when the horizon closed. **Not a success and not a named
    #: failure** — an honest "we do not know", which must stay visible.
    CENSORED = "censored"


#: Dispositions in which the request did NOT get the resource and nothing was arranged. Gated as
#: a sum, because each one on its own can be driven to zero by relabelling into another.
UNRESOLVED_DISPOSITIONS: frozenset[Disposition] = frozenset(
    {Disposition.EXPIRED, Disposition.ABANDONED, Disposition.CENSORED}
)


@dataclass(frozen=True, slots=True)
class LifecycleAudit:
    """One world's reconciliation of arrivals against final dispositions."""

    arrivals: int
    counts: Mapping[Disposition, int]
    #: Request ids that arrived and reached no disposition.
    missing: tuple[str, ...]
    #: Request ids carrying more than one outcome row.
    duplicated: tuple[str, ...]
    #: Request ids carrying an outcome without ever appearing in the arrival stream.
    unexpected: tuple[str, ...]
    #: Request ids whose fate string is not in the family's disposition map.
    unclassified: tuple[str, ...]

    @property
    def resolved(self) -> int:
        return sum(self.counts.values())

    @property
    def unresolved(self) -> int:
        """Expired + abandoned + censored. **The gate quantity.**

        A safety gate on abandonment alone scores -858 and passes for a policy that converted
        those 858 abandonments into 2420 expiries. Gating the sum makes that substitution
        impossible rather than merely visible.
        """
        return sum(self.counts.get(d, 0) for d in UNRESOLVED_DISPOSITIONS)

    @property
    def balances(self) -> bool:
        return not self.problems

    @property
    def problems(self) -> tuple[str, ...]:
        """Every way this world's accounting fails, as human-readable strings."""
        out: list[str] = []
        if self.missing:
            out.append(
                f"{len(self.missing)} request(s) arrived and reached no disposition: "
                f"{', '.join(self.missing[:5])}"
                + (" ..." if len(self.missing) > 5 else "")
            )
        if self.duplicated:
            out.append(
                f"{len(self.duplicated)} request(s) resolved more than once: "
                f"{', '.join(self.duplicated[:5])}"
                + (" ..." if len(self.duplicated) > 5 else "")
            )
        if self.unexpected:
            out.append(
                f"{len(self.unexpected)} outcome(s) for request(s) that never arrived: "
                f"{', '.join(self.unexpected[:5])}"
                + (" ..." if len(self.unexpected) > 5 else "")
            )
        if self.unclassified:
            out.append(
                f"{len(self.unclassified)} outcome(s) carry a fate with no disposition: "
                f"{', '.join(self.unclassified[:5])}"
                + (" ..." if len(self.unclassified) > 5 else "")
            )
        if self.resolved != self.arrivals:
            out.append(
                f"arrivals {self.arrivals} != dispositions {self.resolved} "
                f"({dict((d.value, n) for d, n in sorted(self.counts.items())) })"
            )
        return tuple(out)

    def as_dict(self) -> dict[str, object]:
        return {
            "arrivals": self.arrivals,
            "dispositions": {d.value: n for d, n in sorted(self.counts.items())},
            "unresolved": self.unresolved,
            "missing": list(self.missing),
            "duplicated": list(self.duplicated),
            "unexpected": list(self.unexpected),
            "unclassified": list(self.unclassified),
            "balances": self.balances,
            "problems": list(self.problems),
        }


def audit_lifecycle(
    arrival_ids: Iterable[str],
    outcomes: Iterable[tuple[str, str]],
    dispositions: Mapping[str, Disposition],
) -> LifecycleAudit:
    """Reconcile one world's arrivals against its outcomes.

    ``outcomes`` is ``(request_id, fate)`` pairs — a sequence rather than a mapping, so a
    duplicate can be *detected* here instead of being silently collapsed by a dict on the way in.
    That matters: the families key their outcomes by request id, which makes a double-resolution
    invisible at the call site, and this is the layer that can still see it.

    ``dispositions`` maps the family's own fate strings onto the shared partition. An unmapped
    fate is recorded in ``unclassified`` and excluded from the counts, so a new fate nobody
    classified fails the audit instead of quietly becoming a censoring.
    """
    arrivals = list(arrival_ids)
    arrival_set = set(arrivals)

    counts: dict[Disposition, int] = {d: 0 for d in Disposition}
    seen: dict[str, int] = {}
    unclassified: list[str] = []
    unexpected: list[str] = []

    for request_id, fate in outcomes:
        seen[request_id] = seen.get(request_id, 0) + 1
        if request_id not in arrival_set:
            unexpected.append(request_id)
        disposition = dispositions.get(fate)
        if disposition is None:
            unclassified.append(request_id)
            continue
        counts[disposition] += 1

    missing = tuple(sorted(r for r in arrival_set if r not in seen))
    duplicated = tuple(sorted(r for r, n in seen.items() if n > 1))
    return LifecycleAudit(
        arrivals=len(arrivals),
        counts=counts,
        missing=missing,
        duplicated=duplicated,
        unexpected=tuple(sorted(set(unexpected))),
        unclassified=tuple(sorted(set(unclassified))),
    )


# ---------------------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChargeRow:
    """One participant's settlement row, stripped of everything resource-specific."""

    participant: str
    standing_bid: float
    cost: float
    won: bool


def settlement_problems(
    outcome: str,
    awarded_outcome: str,
    winners: Sequence[str],
    standing_bids: Mapping[str, float],
    charges: Sequence[ChargeRow],
) -> tuple[str, ...]:
    """Check one closed event's charge rows against its declared result.

    Four invariants, none of which the auction code can enforce on itself because each one is a
    statement about the *relationship* between the result and the ledger:

    * **Exactly one charge per participant.** Charging per round would make a three-round auction
      three times the price of a one-round auction for the same allocation.
    * **Every standing bid is charged.** A participant with a live bid that produced no charge
      bid for free, and a policy that finds that state learns to bid into every contest.
    * **No charge without a standing bid.** The converse; a phantom charge.
    * **A no-award event charges no winner.** ``won=True`` on an event that awarded nothing
      credits a win that did not happen, and the outcome factor differs between the two.
    """
    problems: list[str] = []

    counts: dict[str, int] = {}
    for row in charges:
        counts[row.participant] = counts.get(row.participant, 0) + 1
    for participant, n in sorted(counts.items()):
        if n != 1:
            problems.append(f"{participant} charged {n} times; settlement charges exactly once")

    charged = set(counts)
    bidding = {p for p, bid in standing_bids.items() if bid > 0}
    for participant in sorted(bidding - charged):
        problems.append(
            f"{participant} held a standing bid of {standing_bids[participant]:g} and was never "
            "charged"
        )
    for participant in sorted(charged - bidding - set(winners)):
        problems.append(f"{participant} was charged with no standing bid and no award")

    if outcome != awarded_outcome:
        for row in charges:
            if row.won:
                problems.append(
                    f"{row.participant} was charged as a winner on a {outcome!r} event, which "
                    "awarded nothing"
                )
        if winners:
            problems.append(
                f"event outcome is {outcome!r} but names winners {sorted(winners)}"
            )

    for row in charges:
        if row.won and row.participant not in set(winners):
            problems.append(f"{row.participant} charged as a winner but is not in the winner set")
        if not row.won and row.participant in set(winners):
            problems.append(f"{row.participant} is a winner but was charged as a loser")

    return tuple(problems)


__all__ = [
    "UNRESOLVED_DISPOSITIONS",
    "ChargeRow",
    "Disposition",
    "LifecycleAudit",
    "audit_lifecycle",
    "settlement_problems",
]
