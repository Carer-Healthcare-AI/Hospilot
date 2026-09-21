"""Transitions — turning simulated days into something a learner can fit.

One trajectory per **request**, not per auction. A request that loses three auctions and is
finally served has one story with four decisions in it, and the reward belongs to how that
story ended. Splitting it per auction would ask the learner to explain an outcome using only
the last decision that preceded it.

**Credit assignment is terminal, and deliberately so.** Every intermediate decision scores
zero and the request's fate reward lands on its final transition, discounted back by ``gamma``
during the fit. The alternative — inventing a per-round shaping signal — is exactly what
produced a 50-point spread inside one action label in the bed work, where binning it made
things worse rather than better. There is no shaped intermediate reward here because nobody
has one that is defensible.

**Every transition carries the action mask that was live when the action was chosen.** Without
it, an evaluation cannot separate an action the policy declined from one it never had, and a
fit cannot tell an unfitted head from an unavailable one.

**The discount is charged over CLINICAL TIME, not over the decision index.** Every transition
carries :attr:`DiagnosticTransition.elapsed_minutes` -- the clinical interval from this decision
to whatever comes next for the request, and on the terminal row the tail from the last decision
to the moment the fate actually occurred. See :data:`DISCOUNT_HORIZON_MINUTES` for why, and
``qlearn.TRAINING_OBJECTIVE_VERSION`` for the artifact interlock that stops weights fitted under
the old decision-index objective from being served under this one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import AgentKind
from allocation.use_cases.diagnostic_machine.budgets import (
    diagnostic_shift,
    open_diagnostic_budgets,
)
from allocation.use_cases.diagnostic_machine.config import for_modality, rules_version
from allocation.use_cases.diagnostic_machine.contracts import DiagnosticPathwayAction
from allocation.use_cases.diagnostic_machine.encoder import (
    EncodedState,
    encode,
    encoder_version,
)
from allocation.use_cases.diagnostic_machine.masks import (
    ACTIONS,
    INDEX,
    action_space_version,
    mask,
)
from allocation.use_cases.diagnostic_machine.reward import (
    DiagnosticReward,
    DiagnosticRewardObserver,
)
from allocation.use_cases.diagnostic_machine.scenarios import EPOCH, Scenario
from allocation.use_cases.diagnostic_machine.simulator import simulate


#: Minutes of clinical time that one unit of ``gamma`` is charged over.
#:
#: The discount exists to express time preference -- later is worth less. It must therefore be
#: raised over *clinical time*, never over the decision index, because the decision index is
#: something the policy itself chooses: a request kept in the auction for thirty bidding rounds
#: instead of twenty reaches the same fate at the same moment on the same clock, and a
#: decision-index discount pays it for the extra rounds.
#:
#: That is not hypothetical. It was measured in the critical-care family on requests that expired
#: under both arms -- same world, same fate, same raw reward -41.22 -- where a TD policy accrued
#: 1.49x the decision steps of the BASELINE while moving the clock by only 1.03x. Under a
#: decision-index exponent that bought it +3.4 points per expiry for changing nothing, and the
#: fresh Q fit duly learned to stall doomed requests instead of arranging an exit for them.
#:
#: At 60 minutes ``gamma`` reads as a plain per-hour statement ("an outcome an hour from now is
#: worth ``gamma`` of the same outcome now"), and several bidding rounds inside one auction --
#: the same clinical instant -- discount by ``gamma ** 0 == 1``, which is what they should do.
#:
#: Deliberately the same 60 minutes the critical-care family uses. It is a unit of account, not
#: a tuned constant; reward MAGNITUDES are not comparable across families and nothing here
#: compares them.
DISCOUNT_HORIZON_MINUTES = 60.0


def minutes_between(earlier: datetime, later: datetime) -> float:
    """Clinical minutes from ``earlier`` to ``later``, floored at zero."""
    return max((later - earlier).total_seconds() / 60.0, 0.0)


def step_discount(
    gamma: float, elapsed_minutes: float, horizon: float = DISCOUNT_HORIZON_MINUTES
) -> float:
    """``gamma`` charged over ``elapsed_minutes`` of clinical time.

    ``step_discount(g, 0.0) == 1.0`` -- two decisions at the same instant are the same moment and
    nothing is charged between them. ``step_discount(g, horizon) == g``.
    """
    if horizon <= 0:
        raise ValueError("the discount horizon is a duration and must be positive")
    return float(gamma ** (max(elapsed_minutes, 0.0) / horizon))


@dataclass(frozen=True, slots=True)
class DiagnosticTransition:
    """One decision, and what followed it."""

    request_id: str
    agent: AgentKind
    step: int
    state: tuple[float, ...]
    action: int
    action_mask: tuple[bool, ...]
    reward: float
    next_state: tuple[float, ...] | None
    next_mask: tuple[bool, ...] | None
    done: bool
    #: The fate the trajectory ended in. Carried for evaluation, never fed to the fit.
    fate: str = ""
    #: Clinical minutes from this decision to whatever comes next for this request: the next
    #: decision, or -- on a terminal row -- the moment the fate actually occurred. This, not the
    #: decision index, is what the discount is raised over; see :func:`step_discount`.
    elapsed_minutes: float = 0.0

    def as_json(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "agent": self.agent.value,
            "step": self.step,
            "state": list(self.state),
            "action": self.action,
            "action_mask": list(self.action_mask),
            "reward": self.reward,
            "next_state": None if self.next_state is None else list(self.next_state),
            "next_mask": None if self.next_mask is None else list(self.next_mask),
            "done": self.done,
            "fate": self.fate,
            "elapsed_minutes": self.elapsed_minutes,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticDataset:
    """Transitions plus the versions they are only valid under.

    The three version stamps are a safety interlock. Weights fitted under one feature layout
    are meaningless under another and the failure is silent — the vector is still the right
    length and the numbers are still finite.
    """

    transitions: tuple[DiagnosticTransition, ...]
    encoder_version: str
    action_space_version: str
    reward_version: str
    rules_version: str
    scenarios: tuple[str, ...] = ()
    regime: str | None = None

    def __len__(self) -> int:
        return len(self.transitions)

    def action_counts(self) -> dict[str, int]:
        counts = {action.value: 0 for action in ACTIONS}
        for transition in self.transitions:
            counts[ACTIONS[transition.action].value] += 1
        return counts

    def feasibility_counts(self) -> dict[str, int]:
        """How often each action was even available. The denominator for the above."""
        counts = {action.value: 0 for action in ACTIONS}
        for transition in self.transitions:
            for action, ok in zip(ACTIONS, transition.action_mask):
                counts[action.value] += int(ok)
        return counts

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "encoder_version": self.encoder_version,
                    "action_space_version": self.action_space_version,
                    "reward_version": self.reward_version,
                    "rules_version": self.rules_version,
                    "scenarios": list(self.scenarios),
                    "regime": self.regime,
                    "transitions": [t.as_json() for t in self.transitions],
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        return path


@dataclass
class _Collector:
    """Captures decision points, keyed by request, in the order they happened."""

    #: ``(state, action index, mask, when the decision was taken)``. The timestamp is what makes
    #: clinical-time discounting possible at all; without it the only interval available is the
    #: decision count, which the policy chooses rather than the world.
    steps: dict[str, list[tuple[EncodedState, int, tuple[bool, ...], datetime]]] = field(
        default_factory=dict
    )
    agents: dict[str, AgentKind] = field(default_factory=dict)

    def __call__(
        self,
        *,
        agent,
        request,
        round_index,
        utility,
        ceiling,
        view,
        budget,
        options,
        contention,
        max_rounds,
        decision,
    ) -> None:
        state = encode(
            utility=utility,
            ceiling=ceiling,
            round_state=view,
            budget=budget,
            options=options,
            agent=agent,
            max_rounds=max_rounds,
            contention=contention,
            now=options.now,
        )
        allowed = mask(decision.feasible)
        index = INDEX[decision.pathway]
        if not allowed[index]:
            # The policy chose something the mask says was unavailable. That is a real
            # contradiction rather than a rounding issue, and training on it would teach the
            # learner an action it can never legally take.
            raise ValueError(
                f"{decision.pathway.value} was chosen but is not in the feasible set "
                f"{sorted(a.value for a in decision.feasible)}"
            )
        self.steps.setdefault(request.request_id, []).append(
            (state, index, allowed, options.now)
        )
        self.agents[request.request_id] = agent


def build_dataset(
    config: Config,
    scenarios: Sequence[Scenario],
    regime: str | None = "normal",
    regimes: Sequence[str] | None = None,
) -> DiagnosticDataset:
    """Run every scenario, capturing decision points and joining them to fates.

    ``regime`` defaults to ``normal`` rather than to the live pool on purpose: at the live Base
    a scenario burns ~2 % of its allowance, so every transition would come from a world where
    spending is free. A learner trained on that will bid its ceiling every time, because in
    the data it was given, doing so never cost anything.
    """
    # Sampling EVERY regime by default, not just one. Under a comfortable budget no agent is
    # ever forced out by affordability, so `withdraw_unplanned` never appears in the data and
    # the head for it is never fitted — which is precisely how the bed Q-policy ended up with
    # an unfitted zero-valued head winning argmax 17 times for an action exploration could
    # never construct. A learner must see the states it will be asked to act in.
    selected = list(regimes) if regimes is not None else ([regime] if regime else [None])
    observer = DiagnosticRewardObserver()
    transitions: list[DiagnosticTransition] = []

    for scenario, regime in ((s, r) for r in selected for s in scenarios):
        scoped = for_modality(config, scenario.modality)
        shift = diagnostic_shift(EPOCH, hours=8.0)
        budgets = open_diagnostic_budgets(
            scoped, scenario.profile, shift, machines=scenario.machines, regime=regime
        )
        collector = _Collector()
        result = simulate(
            scoped,
            scenario.profile,
            scenario.machines,
            scenario.arrivals,
            budgets,
            starts_at=shift.start,
            ends_at=shift.start + timedelta(hours=scenario.hours),
            alternative_machines=scenario.alternative_machines,
            decision_hook=collector,
        )
        transport = {
            arrival.request.request_id: arrival.request.transport_minutes
            for arrival in scenario.arrivals
        }
        rewards = observer.score_all(result.outcomes, transport)
        fates = {outcome.request_id: outcome.fate for outcome in result.outcomes}
        settled = {outcome.request_id: outcome.resolved_at for outcome in result.outcomes}
        transitions.extend(_stitch(collector, rewards, fates, settled))

    return DiagnosticDataset(
        transitions=tuple(transitions),
        encoder_version=encoder_version(),
        action_space_version=action_space_version(),
        reward_version=observer.version,
        rules_version=rules_version(),
        scenarios=tuple(s.name for s in scenarios),
        regime="+".join(str(r) for r in selected),
    )


def _stitch(
    collector: _Collector,
    rewards: Mapping[str, DiagnosticReward],
    fates: Mapping[str, str],
    resolved_at: Mapping[str, datetime | None] = MappingProxyType({}),
) -> Iterable[DiagnosticTransition]:
    """One trajectory per request, with the fate reward on the terminal step.

    Each row carries its own clinical interval. **The terminal row's interval is the tail from
    the LAST DECISION to the moment the fate actually occurred** -- for an expiry the deadline,
    for an answered request the moment the answer landed. Closing the trajectory at the last
    decision instead would leave that whole wait undiscounted and let a policy bank a bad outcome
    at nearly face value by going quiet: the same defect measured in critical care, where 65% of
    an expiring request's clinical life falls after its final bid.
    """
    for request_id, steps in collector.steps.items():
        reward = rewards.get(request_id)
        terminal = 0.0 if reward is None else reward.total
        agent = collector.agents[request_id]
        fate = fates.get(request_id, "")
        settled = resolved_at.get(request_id)
        for i, (state, action, allowed, now) in enumerate(steps):
            last = i == len(steps) - 1
            next_state, next_mask = (None, None) if last else (
                steps[i + 1][0].values,
                steps[i + 1][2],
            )
            if last:
                gap = 0.0 if settled is None else minutes_between(now, settled)
            else:
                gap = minutes_between(now, steps[i + 1][3])
            yield DiagnosticTransition(
                request_id=request_id,
                agent=agent,
                step=i,
                state=state.values,
                action=action,
                action_mask=allowed,
                reward=terminal if last else 0.0,
                next_state=next_state,
                next_mask=next_mask,
                done=last,
                fate=fate,
                elapsed_minutes=gap,
            )


__all__ = [
    "DISCOUNT_HORIZON_MINUTES",
    "DiagnosticDataset",
    "DiagnosticTransition",
    "build_dataset",
    "minutes_between",
    "step_discount",
]
