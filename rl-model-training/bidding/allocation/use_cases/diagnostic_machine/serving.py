"""Serving a trained Q-policy inside a live diagnostic auction.

The Q-policy scores *pathway actions*; the auction needs a *bid*. This adapter is the join,
and it makes one decision that is worth stating outright:

**The learned policy chooses WHICH action; the ladder still sets HOW MUCH.** When the Q-policy
says compete, the alpha comes from the same four rules the heuristic runs. Nothing in the
dataset teaches a bid size — the reward sees a fate, not an increment — so a head that also
emitted alpha would be inventing a number with no gradient behind it. This keeps the learned
part to the part that was actually learned.

That also makes the comparison in :mod:`.evaluate` honest: heuristic and Q-policy differ in
action selection alone, so a difference in outcomes is attributable to the choice of action
rather than to one of them bidding harder.
"""

from __future__ import annotations

from allocation.config import Config
from allocation.contracts import AgentKind, BudgetState, RoundState
from allocation.use_cases.diagnostic_machine.contracts import DiagnosticPathwayAction
from allocation.use_cases.diagnostic_machine.encoder import encode
from allocation.use_cases.diagnostic_machine.ladder import Verdict, climb
from allocation.use_cases.diagnostic_machine.masks import mask
from allocation.use_cases.diagnostic_machine.policy import (
    DiagnosticDecision,
    DiagnosticHeuristicPolicy,
    DiagnosticOptions,
    DiagnosticPlan,
)
from allocation.use_cases.diagnostic_machine.qlearn import DiagnosticQPolicy
from allocation.use_cases.diagnostic_machine.utility import DiagnosticUtility


class ServedQPolicy:
    """A trained :class:`DiagnosticQPolicy` behind the auction's ``decide`` seam."""

    name = "diagnostic_q"

    def __init__(self, config: Config, policy: DiagnosticQPolicy) -> None:
        self._config = config
        self._q = policy
        # The heuristic is kept as the bid-sizing engine and as the fallback that builds an
        # exit's plan. An exit must name what it arranged, and the Q-policy emits an action
        # label, not a plan.
        self._heuristic = DiagnosticHeuristicPolicy(config)

    def decide(
        self,
        agent: AgentKind,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        options: DiagnosticOptions,
        utility: DiagnosticUtility | None = None,
        contention: float = 1.0,
        max_rounds: int = 3,
        **extra: object,
    ) -> DiagnosticDecision:
        del extra
        if utility is None:
            raise ValueError(
                "a learned policy cannot encode its state without the scored utility; "
                "the auction passes it, so this means the seam was called directly"
            )

        feasible = self._heuristic._feasible(options)
        state = encode(
            utility=utility,
            ceiling=ceiling,
            round_state=round_state,
            budget=budget,
            options=options,
            agent=agent,
            max_rounds=max_rounds,
            contention=contention,
            now=options.now,
        )
        chosen = self._q.decide(state.values, mask(feasible))

        if chosen in (DiagnosticPathwayAction.WIN_NOW, DiagnosticPathwayAction.CONTINUE):
            return self._compete(
                agent, ceiling, round_state, budget, chosen, feasible, options
            )
        return self._exit(chosen, options, feasible)

    # -- competing: the ladder sizes the bid -------------------------------------------------

    def _compete(
        self,
        agent: AgentKind,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        chosen: DiagnosticPathwayAction,
        feasible: frozenset[DiagnosticPathwayAction],
        options: DiagnosticOptions,
    ) -> DiagnosticDecision:
        outcome = climb(
            self._config,
            agent=agent,
            ceiling=ceiling,
            round_state=round_state,
            budget=budget,
            opening_alpha=self._heuristic._opening_alpha(agent),
            lead_alpha=self._heuristic._lead_alpha,
            overtake_margin=self._heuristic._margin,
        )
        if outcome.verdict is Verdict.HOLD:
            return DiagnosticDecision(
                pathway=DiagnosticPathwayAction.CONTINUE, feasible=feasible, holds=True
            )
        if outcome.verdict is Verdict.EXIT:
            # The ladder's exits are MECHANICAL — bid above ceiling, cannot win, cannot
            # afford. They are facts about the auction, not preferences, and a learned policy
            # does not get to override them. It would be bidding a number its budget cannot
            # cover or its utility does not justify.
            # `options` is passed so a forced exit can still ARRANGE something. Dropping
            # it here would record every unaffordable exit as an abandonment and make the
            # Q-policy look reckless for a decision the ladder took on its behalf.
            return self._exit(
                self._best_available_exit(feasible), options, feasible, outcome.reason
            )
        return DiagnosticDecision(pathway=chosen, alpha=outcome.alpha, feasible=feasible)

    # -- exiting: the heuristic builds the plan ----------------------------------------------

    def _exit(
        self,
        chosen: DiagnosticPathwayAction,
        options: DiagnosticOptions | None,
        feasible: frozenset[DiagnosticPathwayAction],
        reason: str = "q-policy selected this exit",
    ) -> DiagnosticDecision:
        """Build the plan the chosen exit commits to, or fall back to an honest abandonment."""
        if options is None or chosen is DiagnosticPathwayAction.WITHDRAW_UNPLANNED:
            return DiagnosticDecision(
                pathway=DiagnosticPathwayAction.WITHDRAW_UNPLANNED, feasible=feasible
            )

        if chosen is DiagnosticPathwayAction.USE_ALTERNATIVE:
            best = options.best_alternative()
            if best is not None:
                modality, value = best
                return DiagnosticDecision(
                    pathway=chosen,
                    plan=DiagnosticPlan(
                        alternative_modality=modality,
                        alternative_yield=value,
                        note=f"{reason}; {modality.value} at yield {value:.2f}",
                    ),
                    feasible=feasible,
                )
        elif chosen is DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY:
            if options.next_capacity_at is not None:
                return DiagnosticDecision(
                    pathway=chosen,
                    plan=DiagnosticPlan(
                        expected_capacity_at=options.next_capacity_at,
                        capacity_probability=options.next_capacity_probability,
                        note=reason,
                    ),
                    feasible=feasible,
                )
        elif chosen is DiagnosticPathwayAction.RE_ENTER_LATER:
            return DiagnosticDecision(
                pathway=chosen,
                plan=DiagnosticPlan(
                    reentry_condition=(
                        "clinical deterioration, or safe diagnostic delay below "
                        f"{self._heuristic._reentry_below:.0f} min"
                    ),
                    note=reason,
                ),
                feasible=feasible,
            )

        # The action was masked in but its plan cannot be built. Recorded as an abandonment
        # rather than as an arranged exit, because nothing was in fact arranged.
        return DiagnosticDecision(
            pathway=DiagnosticPathwayAction.WITHDRAW_UNPLANNED, feasible=feasible
        )

    @staticmethod
    def _best_available_exit(
        feasible: frozenset[DiagnosticPathwayAction],
    ) -> DiagnosticPathwayAction:
        for candidate in (
            DiagnosticPathwayAction.USE_ALTERNATIVE,
            DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY,
            DiagnosticPathwayAction.RE_ENTER_LATER,
        ):
            if candidate in feasible:
                return candidate
        return DiagnosticPathwayAction.WITHDRAW_UNPLANNED


__all__ = ["ServedQPolicy"]
