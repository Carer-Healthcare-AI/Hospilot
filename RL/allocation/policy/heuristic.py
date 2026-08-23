"""The deterministic bidding policy. RL-Steps sections 9-17.

**This is not a placeholder.** There is no trained network, and there cannot be one until
auctions have been logged, the caps have been fitted (B.13) and the reward's largest term has
a source (F-01). This policy runs first, generates the log that makes training possible, and
then stays permanently as the regression baseline and the safety fallback.

It emits ``alpha``, never a point value — ``Increment = alpha x (Ceiling - CurrentBid)``
(section 6). Four rules, each traced to the worked example::

    1  standing bid above ceiling      -> WITHDRAW    section 16: OT holds 105, ceiling drops
                                                      to 94, "no longer rational"
    2  ceiling at or below the leader  -> WITHDRAW    section 12: Ward's ceiling 76 is below
                                                      the standing 85; it cannot win at full
                                                      stretch
    3  leading                         -> alpha 0.25  section 13: "exposes only enough
                                                      priority to establish leadership"
    4  trailing                        -> alpha derived from what overtaking costs

Rule 4 is the one worth noting: section 14's alpha of 0.82 is not a constant, it is what OT
needed to clear a leader at 101 from 75 inside a ceiling of 112. Deriving it rather than
tabulating it is why this policy generalises past the worked example at all.
"""

from __future__ import annotations

from allocation.config import Config
from allocation.contracts import (
    Action,
    AgentKind,
    BudgetState,
    Candidate,
    Decision,
    FeatureSnapshot,
    PathwayOptions,
    PathwayPlan,
    QAction,
    RoundState,
    UtilityBreakdown,
)
from allocation.budget.spend import max_affordable_bid
from allocation.features.scale import clamp

EPS = 1e-9

#: Every action a rule-based policy can reach. Recorded on each decision so an evaluation can
#: tell "the policy declined this exit" from "the exit was not available".
_ALL = frozenset(QAction)


class HeuristicPolicy:
    """Rule-based aggression. Reproduces every bid in END_TO_END section 18.

    Two methods, one set of rules. :meth:`decide` is the narrow bid-mechanics seam;
    :meth:`decide_q` is the full six-action seam and is what the auction calls when pathway
    options are available. ``decide`` delegates to it, so there is one implementation.

    **The bid arithmetic is identical either way, and that is deliberate.** ``decide_q`` adds
    *which* exit and *which* compete regime, never a different alpha — section 18's ladder is
    the regression baseline for the whole system, and a labelling change that moved a bid
    would silently invalidate it. The one lever that can change a bid is
    ``pathway.yaml compete.win_now_alpha_floor``, which is null by default.

    So for this policy ``WIN_NOW`` and ``CONTINUE`` are honest labels over the same
    derivation. Differentiating them by aggression is exactly the thing a learned policy is
    for, and pretending a hand-written rule already does it would put a fabricated
    distinction in the training log.
    """

    name = "heuristic"

    def __init__(self, config: Config) -> None:
        cfg = config.auction["policy"]["heuristic"]
        self._opening = cfg["opening_alpha"]
        self._lead_alpha = float(cfg["lead_alpha"])
        self._margin = float(cfg["overtake_margin"])
        self._config = config
        pathway = config.rule("pathway")
        self._min_release_p = float(pathway["next_release"]["min_probability"])
        compete = pathway.get("compete", {})
        self._win_now_below = compete.get("win_now_below_minutes")
        floor = compete.get("win_now_alpha_floor")
        self._win_now_floor = None if floor is None else float(floor)

    # -- the narrow seam ---------------------------------------------------------------

    def decide(
        self,
        candidate: Candidate,
        utility: UtilityBreakdown,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        snapshot: FeatureSnapshot,
    ) -> tuple[Action, float | None]:
        """Bid mechanics only. Every exit collapses to ``WITHDRAW`` with no onward plan."""
        decision = self.decide_q(
            candidate, utility, ceiling, round_state, budget, snapshot, pathways=None
        )
        return decision.action, decision.alpha

    # -- the full seam -----------------------------------------------------------------

    def decide_q(
        self,
        candidate: Candidate,
        utility: UtilityBreakdown,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        snapshot: FeatureSnapshot,
        pathways: PathwayOptions | None = None,
    ) -> Decision:
        """One of six actions, with the plan the exits commit to.

        ``pathways`` may be ``None`` — a caller on the narrow seam has none to give. Every
        exit is then ``WITHDRAW_UNPLANNED``, which is the truthful record: with nothing known
        about alternatives or releases, nothing was arranged.
        """
        agent = candidate.agent
        mine = self._standing_bid(round_state, agent)
        rival = self._highest_rival(round_state, agent)
        feasible = self._feasible(pathways)

        # 1 · A standing bid above the ceiling is no longer rational. Section 16.
        if mine > ceiling + EPS:
            return self._exit(candidate, pathways, feasible, "standing bid exceeds ceiling")

        # 2 · Cannot win even at full stretch. Section 12.
        if rival > EPS and ceiling <= rival + EPS:
            return self._exit(candidate, pathways, feasible, "ceiling below the leading bid")

        # 2b · Cannot AFFORD to win. AGENT_BUDGET 7.3.
        #
        # Previously invisible: ``apply_guards`` clamped an unaffordable bid to what the budget
        # could cover and the agent carried on bidding a number its utility did not justify.
        # Three things went wrong at once, and all three get worse exactly when the budget
        # binds — which is the regime the whole mechanism is supposed to operate in:
        #
        #   * the auction stopped being decided by clinical need and started being decided by
        #     who still had budget (F-25), because the clamped bid no longer expressed the
        #     patient's utility;
        #   * the agent paid the 0.1 participation charge on a bid that could never win;
        #   * and its patient got nothing arranged, because a clamp is not a decision and had
        #     no pathway attached to it.
        #
        # Exiting instead is both cheaper and more honest, and it is only expressible now that
        # an exit can carry a plan. ``ExitReason.UNAFFORDABLE`` has been defined since the
        # first version of the auction and was never reachable.
        if not self._can_afford_to_compete(budget, round_state, mine, rival):
            return self._exit(
                candidate, pathways, feasible, "budget cannot cover a competitive bid"
            )

        headroom = ceiling - mine
        if headroom <= EPS:
            # At the ceiling with a rival ahead is rule 2; at the ceiling while leading there
            # is nothing left to expose. Holding is a form of continuing, not of winning now.
            return Decision.compete(QAction.CONTINUE, Action.HOLD, None, feasible=feasible)

        # 3 · Opening round — expose enough to lead, no more. Sections 9-10.
        if mine <= EPS and rival <= EPS:
            return self._compete(self._opening_alpha(agent), pathways, feasible)

        # 4 · Leading: a small raise. Trailing: exactly what overtaking costs.
        if mine >= rival - EPS:
            return self._compete(self._lead_alpha, pathways, feasible)

        needed = rival - mine + self._margin
        return self._compete(clamp(needed / headroom), pathways, feasible)

    # -- affordability -----------------------------------------------------------------

    def _can_afford_to_compete(
        self, budget: BudgetState, round_state: RoundState, mine: float, rival: float
    ) -> bool:
        """Could this agent pay for a bid that would actually lead?

        The target is what it takes to *win*, not what it takes to bid. An agent that can
        afford 40 against a leader at 90 cannot compete, and bidding 40 anyway buys nothing
        but a participation charge.

        Deliberately permissive in two places, because a false exit is worse than a false stay:

        * **No rival yet** — only a one-point bid is required, so an agent with almost any
          budget stays in. Round one is sealed; nobody knows what they are bidding against.
        * **No contention on the round view** — falls back to 1.0 rather than refusing. The
          view carries contention on every real bid row, but a first round with no bids yet
          has none to read, and an agent must not exit for lack of a number.
        """
        contention = self._contention(round_state)
        affordable = max_affordable_bid(
            self._config, budget.budget_remaining, contention, won=True
        )
        target = (rival + self._margin) if rival > EPS else 1.0
        # Already committed this much: the exposure is sunk, so only the increment needs
        # covering. Charging an agent again for a bid it is already standing behind would make
        # every leader look unaffordable the moment its budget dipped.
        return affordable >= min(target, max(target - mine, 1.0))

    @staticmethod
    def _contention(round_state: RoundState) -> float:
        live = [b.contention for b in round_state.bids if b.contention is not None]
        return live[0] if live else 1.0

    # -- compete: WIN_NOW or CONTINUE --------------------------------------------------

    def _compete(
        self, alpha: float, pathways: PathwayOptions | None, feasible: frozenset[QAction]
    ) -> Decision:
        """Label the aggression regime by the patient's safe waiting window.

        Straight from RL-Steps' own "When Agent Chooses It" column: ``WIN_NOW`` is for when
        *"delay is dangerous"*, ``CONTINUE`` for when *"immediate acquisition isn't
        essential"* — its example being OT, whose *"safe waiting window is 45 min"*. The
        window is the discriminator the framework itself uses, and
        :func:`~allocation.pathway.options.safe_wait_minutes` is the only thing in the system
        that answers it.

        An unknown window reads as ``WIN_NOW``: a patient nobody can vouch for waiting is not
        a patient to assume can wait.
        """
        wait = getattr(pathways, "safe_wait_minutes", None) if pathways else None
        threshold = self._win_now_below

        if threshold is None:
            urgent = wait is None
        else:
            urgent = wait is None or wait < float(threshold)

        if not urgent:
            return Decision.compete(
                QAction.CONTINUE, Action.INCREASE_BID, alpha, feasible=feasible
            )

        if self._win_now_floor is not None:
            alpha = max(alpha, self._win_now_floor)
        return Decision.compete(QAction.WIN_NOW, Action.INCREASE_BID, alpha, feasible=feasible)

    # -- exit: which of the four -------------------------------------------------------

    def _exit(
        self,
        candidate: Candidate,
        pathways: PathwayOptions | None,
        feasible: frozenset[QAction],
        reason: str,
    ) -> Decision:
        """Choose the exit. Ordered by how much it arranges for the patient.

        The ordering is the clinical one, not a value estimate — this policy ranks nothing,
        and :attr:`Decision.q_values` stays empty rather than carrying invented scores:

        1. **A definitive alternative.** A unit that can hold the patient for the whole
           allocation horizon is a resolution, not a deferral. Nothing further is needed.
        2. **A monitored alternative.** A unit that covers only part of the horizon means the
           patient *will* need a bed again inside it. That is RL-Steps' own re-entry example —
           Ward moves the patient to HDU and re-enters when NEWS2 rises — and it is the
           commonest real case, because HDU's 2.8 h safe hold is shorter than the 4 h horizon.
        3. **A predicted bed.** No alternative, but a release is likely inside the window the
           patient can safely wait.
        4. **Watch in place.** Nothing available and nothing predicted, but the patient can be
           monitored and bidding re-opened if they deteriorate.
        5. **Nothing.** Recorded as ``WITHDRAW_UNPLANNED`` so it can never collect the reward
           terms the arranged exits earn.
        """
        if pathways is None:
            return Decision(
                q_action=QAction.WITHDRAW_UNPLANNED, action=Action.WITHDRAW, feasible=feasible
            )

        best = self._best_alternative(pathways)
        horizon_minutes = self._horizon_minutes(pathways)

        if best is not None:
            covers_horizon = (
                horizon_minutes is not None and best.safe_hold_minutes >= horizon_minutes
            )
            make = getattr(pathways, "make_reentry", None)
            trigger = make(best.unit) if make and not covers_horizon else None

            if trigger is not None:
                return Decision(
                    q_action=QAction.RE_ENTER_LATER,
                    action=Action.WITHDRAW,
                    plan=PathwayPlan(
                        target_unit=best.unit,
                        safe_hold_minutes=best.safe_hold_minutes,
                        reentry=trigger,
                        note=(
                            f"{reason}; held in {best.unit} for "
                            f"{best.safe_hold_minutes:.0f} min, short of the "
                            f"{horizon_minutes:.0f} min horizon — monitored"
                        ),
                    ),
                    feasible=feasible,
                )

            return Decision(
                q_action=QAction.WITHDRAW_ALTERNATIVE,
                action=Action.WITHDRAW,
                plan=PathwayPlan(
                    target_unit=best.unit,
                    safe_hold_minutes=best.safe_hold_minutes,
                    note=f"{reason}; {best.unit} covers the allocation horizon",
                ),
                feasible=feasible,
            )

        if QAction.AWAIT_NEXT_RESOURCE in feasible:
            return Decision(
                q_action=QAction.AWAIT_NEXT_RESOURCE,
                action=Action.WITHDRAW,
                plan=PathwayPlan(
                    expected_release_at=pathways.next_release_at,
                    release_probability=pathways.next_release_probability,
                    note=(
                        f"{reason}; next release p="
                        f"{pathways.next_release_probability:.2f} within "
                        f"{pathways.safe_wait_minutes:.0f} min safe wait"
                    ),
                ),
                feasible=feasible,
            )

        make = getattr(pathways, "make_reentry", None)
        trigger = make(None) if make else None
        if trigger is not None:
            return Decision(
                q_action=QAction.RE_ENTER_LATER,
                action=Action.WITHDRAW,
                plan=PathwayPlan(
                    reentry=trigger,
                    note=f"{reason}; nothing available — monitored in place",
                ),
                feasible=feasible,
            )

        return Decision(
            q_action=QAction.WITHDRAW_UNPLANNED, action=Action.WITHDRAW, feasible=feasible
        )

    # -- feasibility -------------------------------------------------------------------

    def _feasible(self, pathways: PathwayOptions | None) -> frozenset[QAction]:
        """Which actions were available at all.

        Recorded on every decision because an evaluation that cannot separate *declined* from
        *unavailable* will read a policy that never had an alternative as a policy that never
        wanted one.
        """
        always = {QAction.WIN_NOW, QAction.CONTINUE, QAction.WITHDRAW_UNPLANNED}
        if pathways is None:
            return frozenset(always)

        out = set(always)
        if self._best_alternative(pathways) is not None:
            out.add(QAction.WITHDRAW_ALTERNATIVE)

        probability = pathways.next_release_probability
        if probability is not None and probability >= self._min_release_p:
            out.add(QAction.AWAIT_NEXT_RESOURCE)

        make = getattr(pathways, "make_reentry", None)
        if make is not None and (make(None) is not None or QAction.WITHDRAW_ALTERNATIVE in out):
            out.add(QAction.RE_ENTER_LATER)
        return frozenset(out)

    @staticmethod
    def _best_alternative(pathways: PathwayOptions):
        """The best usable alternative, via the options object's own rule."""
        best = getattr(pathways, "best_alternative", None)
        if best is not None:
            return best
        return next((o for o in pathways.alternatives if o.usable), None)

    def _horizon_minutes(self, pathways: PathwayOptions) -> float | None:
        """The allocation horizon in minutes, read from the reward's own window.

        ``reward.horizon_hours`` is what "inside the window" means everywhere else in the
        system — it is the period the outcome is scored over. Using it here keeps "covers the
        horizon" and "scored within the horizon" the same four hours.
        """
        try:
            return float(self._config.reward["horizon_hours"]) * 60.0
        except (KeyError, TypeError, ValueError):
            return None

    # -- helpers -------------------------------------------------------------------

    def _opening_alpha(self, agent: AgentKind) -> float:
        return float(self._opening.get(agent.value, self._opening["default"]))

    @staticmethod
    def _standing_bid(round_state: RoundState, agent: AgentKind) -> float:
        for bid in round_state.bids:
            if bid.agent is agent:
                return 0.0 if bid.action is Action.WITHDRAW else bid.amount
        return 0.0

    @staticmethod
    def _highest_rival(round_state: RoundState, agent: AgentKind) -> float:
        """The best standing bid held by someone else.

        Excluding the agent's own bid matters: section 13 has ER reading "highest opponent =
        75" while itself holding 85. Reading its own bid as the target would make a leader
        chase itself.
        """
        rivals = [
            bid.amount
            for bid in round_state.bids
            if bid.agent is not agent and bid.action is not Action.WITHDRAW
        ]
        return max(rivals, default=0.0)
