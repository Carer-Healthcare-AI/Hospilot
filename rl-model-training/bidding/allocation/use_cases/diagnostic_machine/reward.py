"""The diagnostic reward — what a policy is actually being asked to maximise.

Two defects in the bed reward were verified rather than suspected, and both are fixed here
rather than inherited:

**It had no cost term.** A win was scored on its clinical outcome alone, so a policy paying
118 points for a bed and one paying 60 for the same bed received the same reward. Nothing in
the objective preferred the cheaper win, and the budget mechanism — the entire price signal —
was invisible to the learner. Here, ``budget_cost`` is a term.

**It could not see a no-award.** Four terms were hardcoded ``True`` on a win, so the minimum
reward for winning was exactly 80 points, and the objective's clearest instruction was
therefore "win, at any price". An auction that awarded nothing produced no signal at all.
Here, every fate scores, and :attr:`DiagnosticReward.abandonment` is negative.

**Nothing here is fitted.** The weights are a stated shape — clinical value dominant, delay
and abandonment materially negative, cost small but non-zero — and every one is an assumption
recorded in ``config/reward.yaml`` with its own status. What the shape encodes deliberately:
answering the question is worth far more than answering it cheaply, but answering it cheaply
is worth more than answering it expensively, which is the ordering the bed reward could not
express at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from allocation.use_cases.diagnostic_machine.config import diagnostic_reward_rules
from allocation.use_cases.diagnostic_machine.simulator import Fate, RequestOutcome


@dataclass(frozen=True, slots=True)
class DiagnosticReward:
    """One request's reward, decomposed. Every term is retained, never just the sum.

    A reward with no record of which term moved it cannot be re-derived after a weight change,
    and cannot be argued with by anyone who was not in the room when it was written.
    """

    request_id: str
    #: Expected management impact delivered, in time.
    clinical_value: float = 0.0
    #: How much of the safe window was still unused when the answer landed.
    timeliness: float = 0.0
    #: The question was answered by another modality that could genuinely answer it.
    diverted_value: float = 0.0
    #: Waiting, as a fraction of the safe diagnostic delay consumed.
    delay_penalty: float = 0.0
    #: Points actually spent winning. The term the bed reward did not have.
    budget_cost: float = 0.0
    #: Moving the patient to the machine.
    transport_cost: float = 0.0
    #: The safe window elapsed with nothing arranged.
    expiry_penalty: float = 0.0
    #: The request left the auction with nothing arranged at all.
    abandonment: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.clinical_value
            + self.timeliness
            + self.diverted_value
            + self.delay_penalty
            + self.budget_cost
            + self.transport_cost
            + self.expiry_penalty
            + self.abandonment
        )

    def terms(self) -> Mapping[str, float]:
        return {
            "clinical_value": self.clinical_value,
            "timeliness": self.timeliness,
            "diverted_value": self.diverted_value,
            "delay_penalty": self.delay_penalty,
            "budget_cost": self.budget_cost,
            "transport_cost": self.transport_cost,
            "expiry_penalty": self.expiry_penalty,
            "abandonment": self.abandonment,
        }


class DiagnosticRewardObserver:
    """Scores request outcomes. Stateless apart from its weights."""

    def __init__(self, weights: Mapping[str, float] | None = None) -> None:
        rules = diagnostic_reward_rules()
        self.version = str(rules.get("version", "unversioned"))
        self.status = str(rules.get("status", "unknown"))
        self._w = dict(rules["weights"])
        if weights:
            self._w.update(weights)

    def score(self, outcome: RequestOutcome, transport_minutes: int = 0) -> DiagnosticReward:
        """Reward for one settled request.

        Every fate scores, including the ones that produced no procedure. A fate with no
        reward is a fate the learner cannot be taught to avoid.
        """
        w = self._w
        procedure = outcome.procedure

        if outcome.fate == Fate.ANSWERED and procedure is not None:
            window = max(
                (procedure.latest_useful_at - procedure.requested_at).total_seconds() / 60.0,
                1e-9,
            )
            slack = max(procedure.deadline_slack_minutes, 0.0) / window
            consumed = min(procedure.delay_minutes / window, 1.0)
            return DiagnosticReward(
                request_id=outcome.request_id,
                clinical_value=w["answered"] * outcome.requested_value,
                timeliness=w["timeliness"] * slack,
                delay_penalty=-w["delay"] * consumed,
                budget_cost=-w["budget_cost"] * procedure.winning_bid,
                transport_cost=-w["transport"] * min(transport_minutes / 60.0, 1.0),
            )

        if outcome.fate == Fate.ANSWERED_LATE and procedure is not None:
            # The scan happened and the answer was useless. Scored as the cost of a procedure
            # with none of the value — deliberately worse than never having run it.
            return DiagnosticReward(
                request_id=outcome.request_id,
                delay_penalty=-w["delay"],
                budget_cost=-w["budget_cost"] * procedure.winning_bid,
                transport_cost=-w["transport"] * min(transport_minutes / 60.0, 1.0),
            )

        if outcome.fate == Fate.DIVERTED:
            # A real handover preserved most of the question's value at no machine cost. Worth
            # less than the right scan, worth far more than an abandonment — and the mechanism
            # must be able to prefer it, which requires it to be positive.
            return DiagnosticReward(
                request_id=outcome.request_id,
                diverted_value=w["diverted"] * outcome.requested_value,
            )

        if outcome.fate == Fate.EXPIRED:
            return DiagnosticReward(
                request_id=outcome.request_id,
                expiry_penalty=-w["expired"] * outcome.requested_value,
                delay_penalty=-w["delay"],
            )

        if outcome.fate == Fate.ABANDONED:
            # The expiry terms PLUS a surcharge. An abandoned request lost its answer exactly
            # as an expired one did; the surcharge is for having decided to stop trying. Built
            # from the expiry terms rather than from an independent weight so that
            # "abandonment is worse than expiry" cannot be broken by retuning either number.
            return DiagnosticReward(
                request_id=outcome.request_id,
                expiry_penalty=-w["expired"] * outcome.requested_value,
                delay_penalty=-w["delay"],
                abandonment=-w["abandonment_surcharge"] * outcome.requested_value,
            )

        # PENDING: the run ended, not the request. Scored zero rather than penalised — a
        # boundary of the simulation is not a decision the policy made.
        return DiagnosticReward(request_id=outcome.request_id)

    def score_all(
        self,
        outcomes: tuple[RequestOutcome, ...],
        transport: Mapping[str, int] | None = None,
    ) -> dict[str, DiagnosticReward]:
        transport = transport or {}
        return {
            outcome.request_id: self.score(outcome, transport.get(outcome.request_id, 0))
            for outcome in outcomes
        }


def episode_return(rewards: Mapping[str, DiagnosticReward]) -> float:
    return sum(reward.total for reward in rewards.values())


__all__ = [
    "DiagnosticReward",
    "DiagnosticRewardObserver",
    "episode_return",
]
