"""Which pathway actions the ladder can actually let happen at a decision point.

A port of ``critical_care_equipment.ladder_reachability``, action-for-action. Duplicated rather
than shared, per this package's own established precedent (see ``ladder.py``'s docstring): every
bed- or cross-family-coupled helper gets a per-use-case equivalent. What that costs, stated
plainly: the two copies can drift if someone edits one. ``ladder_image``'s branch structure is
pinned against ``oracle_fork.build_forced_decision`` by a test for exactly that reason.

**The defect this exists to close.** ``oracle_fork.build_forced_decision`` sizes and gates a
chosen ``WIN_NOW``/``CONTINUE`` through :func:`~.ladder.climb`, and climb's ``EXIT`` and ``HOLD``
verdicts **replace** the chosen action:

===================  ==========================================================
climb verdict        what a chosen WIN_NOW / CONTINUE becomes
===================  ==========================================================
``COMPETE``          itself
``HOLD``             ``CONTINUE`` (so ``WIN_NOW`` is unreachable)
``EXIT``             ``_best_available_exit(feasible)``
===================  ==========================================================

The other four actions pass through untouched.

Two things break when the raw feasible set is used instead of the reachable one, and both are
the reasons critical care's port was written:

1. **The recorded action is not the drawn action.** ``UniformDiagnosticExplorer`` draws
   uniformly from the feasible set, so its propensity is only known exactly if the draw is what
   executes. Where the ladder overrides, the recorded action is something the explorer never
   drew, and any closed-form propensity over the feasible set is wrong.

2. **The Bellman max ranges over impossible actions.** ``max_{a' feasible} Q(s', a')`` includes
   ``WIN_NOW``/``CONTINUE`` at states where the ladder has already decided to exit or hold.
   Those pairs can never be executed, so they carry no data, so their value is pure
   extrapolation — and ``max`` systematically selects the largest extrapolation. In critical
   care this was measured as the ``+18.44`` bootstrapping-head inflation, and it is why the
   absorbing heads (which take no max) were accurate to ``-0.33`` while every bootstrapping
   head was not.

The map is **deterministic in the decision state**, so everything here is exact. Nothing is
estimated and nothing is fitted. This module changes no reward, no gate, no encoder column and
no action-space membership. It narrows a mask to the actions the surrounding machinery could
actually produce.
"""

from __future__ import annotations

from typing import Iterable

from allocation.config import Config
from allocation.contracts import AgentKind, BudgetState, RoundState
from allocation.use_cases.diagnostic_machine.contracts import DiagnosticPathwayAction
from allocation.use_cases.diagnostic_machine.ladder import Verdict, climb

#: The two actions the ladder can override. Everything else passes through
#: ``build_forced_decision`` untouched.
COMPETE_ACTIONS: frozenset[DiagnosticPathwayAction] = frozenset(
    {DiagnosticPathwayAction.WIN_NOW, DiagnosticPathwayAction.CONTINUE}
)


def best_available_exit(
    feasible: Iterable[DiagnosticPathwayAction],
) -> DiagnosticPathwayAction:
    """``oracle_fork._best_available_exit``, restated so this module imports no private name."""
    members = frozenset(feasible)
    for candidate in (
        DiagnosticPathwayAction.USE_ALTERNATIVE,
        DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY,
        DiagnosticPathwayAction.RE_ENTER_LATER,
    ):
        if candidate in members:
            return candidate
    return DiagnosticPathwayAction.WITHDRAW_UNPLANNED


def ladder_verdict(
    config: Config,
    agent: AgentKind,
    ceiling: float,
    round_state: RoundState,
    budget: BudgetState,
) -> Verdict:
    """The verdict ``build_forced_decision`` will apply to a chosen compete action.

    Calls the same :func:`~.ladder.climb` with the same three tuning inputs the fork does, read
    from the same config block, so the two cannot disagree.
    """
    from allocation.use_cases.diagnostic_machine.oracle_fork import (
        _lead_alpha,
        _opening_alpha,
        _overtake_margin,
    )

    return climb(
        config,
        agent=agent,
        ceiling=ceiling,
        round_state=round_state,
        budget=budget,
        opening_alpha=_opening_alpha(config, agent),
        lead_alpha=_lead_alpha(config),
        overtake_margin=_overtake_margin(config),
    ).verdict


def ladder_image(
    action: DiagnosticPathwayAction,
    verdict: Verdict,
    exit_to: DiagnosticPathwayAction,
) -> DiagnosticPathwayAction:
    """What ``action`` becomes once the ladder has had its say. Exactly
    ``build_forced_decision``'s branch structure, as a pure function."""
    if action not in COMPETE_ACTIONS:
        return action
    if verdict is Verdict.EXIT:
        return exit_to
    if verdict is Verdict.HOLD:
        return DiagnosticPathwayAction.CONTINUE
    return action


def reachable_actions(
    config: Config,
    agent: AgentKind,
    ceiling: float,
    round_state: RoundState,
    budget: BudgetState,
    feasible: Iterable[DiagnosticPathwayAction],
) -> frozenset[DiagnosticPathwayAction]:
    """The image of ``feasible`` under the ladder — every action that can actually be executed.

    A policy choosing from this set is never overridden, so the action it picks is the action
    that happens.

    Never empty: ``WITHDRAW_UNPLANNED`` passes through and is always feasible, and under every
    verdict at least one compete action or its image is present.
    """
    members = frozenset(feasible)
    if not members:
        return frozenset()
    if not (members & COMPETE_ACTIONS):
        # No compete action was feasible, so climb is never consulted and nothing is overridden.
        return members
    verdict = ladder_verdict(config, agent, ceiling, round_state, budget)
    exit_to = best_available_exit(members)
    return frozenset(ladder_image(a, verdict, exit_to) for a in members)


def overridden(
    config: Config,
    agent: AgentKind,
    ceiling: float,
    round_state: RoundState,
    budget: BudgetState,
    feasible: Iterable[DiagnosticPathwayAction],
    chosen: DiagnosticPathwayAction,
) -> bool:
    """Whether ``chosen`` would be replaced by the ladder. Diagnostic instrumentation only — a
    policy that picks from :func:`reachable_actions` should never make this true."""
    members = frozenset(feasible)
    if chosen not in COMPETE_ACTIONS or not (members & COMPETE_ACTIONS):
        return False
    verdict = ladder_verdict(config, agent, ceiling, round_state, budget)
    return ladder_image(chosen, verdict, best_available_exit(members)) is not chosen


__all__ = [
    "COMPETE_ACTIONS",
    "best_available_exit",
    "ladder_image",
    "ladder_verdict",
    "overridden",
    "reachable_actions",
]
