"""Resource-neutral lifecycle vocabulary for pathway actions.

**This module holds no domain model.** It has no notion of a bed, a scanner or a ventilator,
and imports nothing from :mod:`allocation.use_cases`. What it defines is a *contract*: the two
questions every use case's pathway-action enum must answer, and the two derived sets every
algorithm that reasons about trajectories needs to name.

The two questions are genuinely different, and conflating them is a measured defect rather
than a stylistic one:

``leaves_auction``     the request stops bidding in the auction it is currently in. Its bid
                      mechanic is :attr:`~allocation.contracts.Action.WITHDRAW`. It may or may
                      not still be a live request afterwards.
``terminates_request``  the request's life ends. No further decision will ever be asked for
                      it, and it has received exactly one final outcome.

In critical care, ``AWAIT_RELEASE`` and ``RE_ENTER_LATER`` leave the auction and stay queued —
measured on shift seeds 901-905, 465 of 516 takings of those two actions were followed by a
further decision for the same request. ``USE_ALTERNATIVE`` and ``WITHDRAW_UNPLANNED`` end it
(0 of 24 had a later decision). In bed allocation the split is different again:
``WITHDRAW_UNPLANNED`` returns the patient to the pool (``ParticipationLedger`` sets them
``ACTIVE``), so only ``WITHDRAW_ALTERNATIVE`` terminates. **Terminality is therefore owned by
the use case and cannot be inferred from the bid mechanic**, which is exactly why the predicate
has to be declared per family rather than derived once here.

Nothing in this module decides which actions are which. It names the questions, derives the
sets, and refuses a set of answers that cannot describe a lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, TypeVar, runtime_checkable


@runtime_checkable
class PathwayLifecycle(Protocol):
    """What every use case's pathway-action enum must be able to answer.

    ``value`` is part of the contract, not incidental: the error messages below name the offending
    action, and every family's action enum is a ``str`` enum whose value is what reaches the audit
    log. Declaring it here is also what lets the helpers below type-check without suppressions.
    """

    @property
    def value(self) -> str:
        """The action's serialised name, as it appears in an audit row."""

    @property
    def leaves_auction(self) -> bool:
        """True when this action stops the request bidding in the auction it is in now."""

    @property
    def terminates_request(self) -> bool:
        """True when this action ends the request's life, outcome included.

        A terminating action must also leave the auction: a request cannot be finished with
        and still bidding. :func:`lifecycle_errors` enforces that.
        """


#: Bound to :class:`PathwayLifecycle` rather than to :class:`~enum.Enum`, so the helpers below
#: read the two predicates through the declared contract instead of through a suppression. Any
#: enum satisfying the protocol binds; an enum missing a predicate is a type error at the call
#: site, which is where it should be caught.
ActionT = TypeVar("ActionT", bound=PathwayLifecycle)


def auction_exit_actions(actions: Iterable[ActionT]) -> frozenset[ActionT]:
    """The actions that leave the current auction, terminal or not.

    The predicate to stratify or mask on when the question is *"does the request stop being
    asked for decisions in THIS auction event"* — bid mechanics, round accounting, and the
    per-decision truncation an explorer's depth model is built on.
    """
    return frozenset(a for a in actions if a.leaves_auction)


def terminal_actions(actions: Iterable[ActionT]) -> frozenset[ActionT]:
    """The actions that end the request's life.

    The predicate to use when the question is *"is this trajectory absorbing"* — lifecycle
    accounting, fate assignment, double-outcome detection, and any claim about trajectory
    depth being geometric.
    """
    return frozenset(a for a in actions if a.terminates_request)


def lifecycle_errors(actions: Iterable[ActionT]) -> tuple[str, ...]:
    """Structural problems with one family's answers, as human-readable strings.

    Three rules, each of which has a failure mode behind it:

    * **A terminating action must leave the auction.** Otherwise the request is finished with
      and still bidding, and the auction can award a resource to an outcome already written.
    * **At least one action must stay in the auction.** An action space where every choice
      leaves cannot produce a multi-round auction at all.
    * **At least one action must terminate.** A family where nothing ends a request has no
      absorbing state, so no trajectory ever closes and no fate is ever final.

    Returned rather than raised so a caller can report every problem at once; the invariant
    tests assert the tuple is empty.
    """
    ordered = list(actions)
    problems: list[str] = []
    for action in ordered:
        if action.terminates_request and not action.leaves_auction:
            problems.append(
                f"{action.value!r} terminates the request but does not leave the auction; "
                "a finished request must not still be bidding"
            )
    if ordered and not any(not a.leaves_auction for a in ordered):
        problems.append(
            "every action leaves the auction; no multi-round bidding is possible in this "
            "action space"
        )
    if ordered and not any(a.terminates_request for a in ordered):
        problems.append(
            "no action terminates a request; without an absorbing action no trajectory ever "
            "closes and no fate is ever final"
        )
    return tuple(problems)


__all__ = [
    "PathwayLifecycle",
    "auction_exit_actions",
    "lifecycle_errors",
    "terminal_actions",
]
