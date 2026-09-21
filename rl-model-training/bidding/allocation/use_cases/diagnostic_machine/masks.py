"""Valid-action masking over :class:`DiagnosticPathwayAction`.

**A mask is not an optimisation.** Without one, an unfitted head is a zero-valued default that
wins ``argmax`` whenever every fitted head is negative — which is how the bed Q-policy came to
emit ``WITHDRAW_UNPLANNED`` seventeen times for an action ε-greedy exploration could never
have constructed and the fit had therefore never seen. The action was not *chosen*; it was
what remained when nothing else scored above zero.

Masking closes that off structurally: an action the world does not offer cannot be selected,
cannot accumulate a value, and cannot be credited with an outcome. The mask is recorded on
every transition so an evaluation can tell a declined action from an unavailable one.

The ordering in :data:`ACTIONS` is load-bearing — it indexes the Q-value vector — and is
covered by the encoder version hash's sibling, :func:`action_space_version`.
"""

from __future__ import annotations

import hashlib

from allocation.use_cases.diagnostic_machine.contracts import DiagnosticPathwayAction
from allocation.use_cases.diagnostic_machine.policy import DiagnosticOptions

#: The action space, in index order.
ACTIONS: tuple[DiagnosticPathwayAction, ...] = (
    DiagnosticPathwayAction.WIN_NOW,
    DiagnosticPathwayAction.CONTINUE,
    DiagnosticPathwayAction.USE_ALTERNATIVE,
    DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY,
    DiagnosticPathwayAction.RE_ENTER_LATER,
    DiagnosticPathwayAction.WITHDRAW_UNPLANNED,
)

INDEX = {action: i for i, action in enumerate(ACTIONS)}


def action_space_version() -> str:
    return hashlib.sha256("|".join(a.value for a in ACTIONS).encode()).hexdigest()[:12]


def feasible_actions(
    options: DiagnosticOptions,
    min_capacity_probability: float,
) -> frozenset[DiagnosticPathwayAction]:
    """Which actions the world actually offers at this decision point.

    Three are always available: an agent may always compete, and may always give up. The three
    strategic exits each require something real —

    ``USE_ALTERNATIVE``      a modality that answers enough of the question AND has a slot in
                             time (``DiagnosticOptions.best_alternative``)
    ``AWAIT_NEXT_CAPACITY``  a scheduled start, confident enough, before the deadline
    ``RE_ENTER_LATER``       a patient who can be monitored, and time left to monitor them in

    — and where the requirement is not met the action is absent, not merely unattractive.
    """
    out = {
        DiagnosticPathwayAction.WIN_NOW,
        DiagnosticPathwayAction.CONTINUE,
        DiagnosticPathwayAction.WITHDRAW_UNPLANNED,
    }
    if options.best_alternative() is not None:
        out.add(DiagnosticPathwayAction.USE_ALTERNATIVE)

    probability = options.next_capacity_probability
    if (
        options.next_capacity_at is not None
        and probability is not None
        and probability >= min_capacity_probability
        and options.next_capacity_at <= options.request.latest_useful_at
    ):
        out.add(DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY)

    if options.monitorable and options.safe_wait_minutes > 0:
        out.add(DiagnosticPathwayAction.RE_ENTER_LATER)
    return frozenset(out)


def mask(feasible: frozenset[DiagnosticPathwayAction]) -> tuple[bool, ...]:
    """``feasible`` as a boolean vector in :data:`ACTIONS` order."""
    if not feasible:
        raise ValueError(
            "an empty action mask leaves a policy with nothing to choose; "
            "WITHDRAW_UNPLANNED is always available and must be present"
        )
    return tuple(action in feasible for action in ACTIONS)


def apply(values: list[float], allowed: tuple[bool, ...]) -> list[float]:
    """Q-values with masked entries driven to negative infinity.

    ``-inf`` rather than a large negative constant: a constant is still a number a sufficiently
    negative fitted head can beat, and the whole point is that it cannot be beaten.
    """
    if len(values) != len(ACTIONS) or len(allowed) != len(ACTIONS):
        raise ValueError(f"expected {len(ACTIONS)} q-values and mask entries")
    return [v if ok else float("-inf") for v, ok in zip(values, allowed)]


def argmax(values: list[float], allowed: tuple[bool, ...]) -> DiagnosticPathwayAction:
    """The best *available* action. Ties break on index order, for determinism."""
    masked = apply(values, allowed)
    best = max(range(len(ACTIONS)), key=lambda i: (masked[i], -i))
    if masked[best] == float("-inf"):  # pragma: no cover - mask() forbids an empty mask
        raise ValueError("no action is available")
    return ACTIONS[best]


__all__ = [
    "ACTIONS",
    "INDEX",
    "action_space_version",
    "apply",
    "argmax",
    "feasible_actions",
    "mask",
]
