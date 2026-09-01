"""Building the :class:`PathwayPlan` an exit commits to. One implementation, every caller.

Three things need to turn a chosen exit into a plan: the heuristic, the learned policy, and the
epsilon-greedy explorer that picks an exit at random. They were each doing it separately, which
is how the explorer came to reach into ``LinearQPolicy._plan`` and crash on a policy that had no
such method.

Sharing it matters for more than tidiness. A plan is what makes an exit *attributable* — it is
the difference between a hand-off the reward can credit and an abandonment it must not. Three
implementations of that would eventually disagree, and the disagreement would show up as a
reward paid for care nobody arranged.
"""

from __future__ import annotations

from allocation.contracts import PathwayOptions, PathwayPlan, QAction


def build_plan(
    action: QAction, pathways: PathwayOptions | None, note: str = ""
) -> PathwayPlan | None:
    """The plan for ``action``, or ``None`` when it cannot be arranged.

    ``None`` is a real answer, not a failure: it means the caller must fall back to
    ``WITHDRAW_UNPLANNED``, because :class:`Decision` refuses to construct a strategic exit
    without its plan. That refusal is the guarantee the whole Q-space rests on.
    """
    if action is QAction.WITHDRAW_UNPLANNED or not action.exits or pathways is None:
        return None

    best = getattr(pathways, "best_alternative", None)

    if action is QAction.WITHDRAW_ALTERNATIVE:
        if best is None:
            return None
        return PathwayPlan(
            target_unit=best.unit,
            safe_hold_minutes=best.safe_hold_minutes,
            note=note or f"exit to {best.unit}",
        )

    if action is QAction.AWAIT_NEXT_RESOURCE:
        if pathways.next_release_at is None or pathways.next_release_probability is None:
            return None
        return PathwayPlan(
            expected_release_at=pathways.next_release_at,
            release_probability=pathways.next_release_probability,
            note=note or "wait on predicted release",
        )

    # RE_ENTER_LATER. The holding unit is optional — "wait and watch" is a real exit, and
    # requiring one would make this action available only to patients who already had an
    # alternative, collapsing it into WITHDRAW_ALTERNATIVE.
    make = getattr(pathways, "make_reentry", None)
    trigger = make(best.unit if best else None) if make else None
    if trigger is None:
        return None
    return PathwayPlan(
        target_unit=best.unit if best else None,
        safe_hold_minutes=best.safe_hold_minutes if best else None,
        reentry=trigger,
        note=note or "monitored exit",
    )
