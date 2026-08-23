"""Running the world, scoring the outcomes, emitting trainable episodes.

This is where the four essentials meet::

    reward    every auction is scored through reward/observer.score, mortality included,
              so `Episode.complete` is True and `trainable()` returns something
    data      many shifts x many auctions, seeded and reproducible
    learning  each step carries the encoded state and the action taken — the (s, a, r) the
              trainer consumes
    pilot     every episode is stamped with the fabrication version it was generated under

**The completeness rule is not relaxed.** ``reward/episode.py``: *"An episode containing any
incomplete auction is itself incomplete. Silently dropping the unscored steps would tell the
policy that a shift with an unobserved death went fine."* That still holds here. What changes is
only that mortality is *observable* in a simulated world, so completeness is achievable rather
than permanently out of reach. If a term goes unobserved for some other reason, the episode is
still discarded — :func:`generate` reports how many, and a run whose completeness rate is not
~1.0 has a bug worth finding before any policy is trained on it.

**Every episode carries three versions**: ``caps_version`` (what the utilities were denominated
in), ``encoder_version`` (what the state vector means) and ``fabrication_version`` (what world
it happened in). A policy is only valid for all three, and pooling episodes across any of them
produces a training set whose examples are not about the same thing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random
from typing import Iterable, Mapping, Sequence

from allocation.budget.base import derive_all
from allocation.budget.factors import compute_factors
from allocation.budget.ledger import open_shift, recover
from allocation.budget.shifts import resolve_shift
from allocation.config import Config
from allocation.contracts import (
    AgentKind,
    AuctionMode,
    BiddingPolicy,
    BudgetState,
    QAction,
)
from allocation.pathway.participation import ParticipationLedger
from allocation.reward.episode import Episode, Step, build_episode, trainable
from allocation.reward.terms import discount_gamma
from allocation.sim.fabricated import DEFAULT, FabricationRegister
from allocation.sim.outcomes import Fate, resolve, score_auction, score_for_agent
from allocation.sim.world import SimDataSource, SimWorld
from allocation.trigger.runtime import run_allocation


@dataclass(frozen=True, slots=True)
class Transition:
    """One agent's decision in one auction, with everything a trainer needs.

    Stored per *agent per auction*, not per round. The budget is a shift-level pacing mechanism
    and §21's objective is the discounted return over a shift, so the decision that matters is
    "how hard did this department push for this bed", not each individual increment. Per-round
    transitions would make the credit assignment finer than the reward, which is measured once
    per auction four hours later.
    """

    auction_id: str
    shift_id: str
    agent: AgentKind
    candidate_id: str
    state: tuple[float, ...]
    q_action: QAction
    alpha: float | None
    won: bool
    bid: float
    utility: float
    ceiling: float
    cost: float
    reward: float
    complete: bool
    feasible: tuple[str, ...]
    budget_remaining: float
    burn_rate: float
    #: The state this agent faced at its NEXT auction in the same shift, and the actions
    #: available there. ``None`` at the end of a shift.
    #:
    #: **Without this there is no reinforcement learning**, only supervised regression onto
    #: observed returns. The Bellman target ``r + gamma * max_a' Q(s', a')`` needs s', and the
    #: episode boundary is the shift because RL-Steps section 21's objective is the discounted
    #: return over a shift: *"an agent that bids hard at 09:00 and has nothing left at 17:00
    #: for a trauma arrival made a bad decision at 09:00"*. That sentence is only learnable if
    #: the 09:00 transition can see the 17:00 state through a chain of bootstraps.
    next_state: tuple[float, ...] | None = None
    next_feasible: tuple[str, ...] = ()
    #: True when this was the agent's last auction of the shift, so the target is ``r`` alone.
    #: Bootstrapping past a terminal state would credit a decision with a return earned in a
    #: shift whose budget had already been reset.
    terminal: bool = True

    def as_dict(self) -> Mapping[str, object]:
        return {
            "auction_id": self.auction_id,
            "shift_id": self.shift_id,
            "agent": self.agent.value,
            "candidate_id": self.candidate_id,
            "state": list(self.state),
            "q_action": self.q_action.value,
            "alpha": self.alpha,
            "won": self.won,
            "bid": self.bid,
            "utility": self.utility,
            "ceiling": self.ceiling,
            "cost": self.cost,
            "reward": self.reward,
            "complete": self.complete,
            "feasible": list(self.feasible),
            "budget_remaining": self.budget_remaining,
            "burn_rate": self.burn_rate,
            "next_state": list(self.next_state) if self.next_state else None,
            "next_feasible": list(self.next_feasible),
            "terminal": self.terminal,
        }


@dataclass(frozen=True, slots=True)
class Dataset:
    """Everything one generation run produced."""

    episodes: tuple[Episode, ...]
    transitions: tuple[Transition, ...]
    caps_version: str
    encoder_version: str
    fabrication_version: str
    seed: int
    auctions: int
    abandonments: int

    @property
    def complete_episodes(self) -> tuple[Episode, ...]:
        return trainable(self.episodes)

    @property
    def completeness(self) -> float:
        """Fraction of episodes usable for training.

        RL_READINESS calls this *"the honest headline metric"*. Against the real schema it is
        0.0 and permanently so (F-01). Here it should be ~1.0, and anything lower is a bug in
        the outcome model rather than a property of the world.
        """
        if not self.episodes:
            return 0.0
        return len(self.complete_episodes) / len(self.episodes)

    def summary(self) -> str:
        wins: dict[str, int] = {}
        actions: dict[str, int] = {}
        for t in self.transitions:
            actions[t.q_action.value] = actions.get(t.q_action.value, 0) + 1
            if t.won:
                wins[t.agent.value] = wins.get(t.agent.value, 0) + 1
        total_wins = sum(wins.values()) or 1
        lines = [
            f"seed                 {self.seed}",
            f"auctions             {self.auctions}",
            f"episodes             {len(self.episodes)}  "
            f"({len(self.complete_episodes)} trainable, {self.completeness:.0%})",
            f"transitions          {len(self.transitions)}",
            f"abandonments         {self.abandonments}",
            f"caps_version         {self.caps_version}",
            f"encoder_version      {self.encoder_version}",
            f"fabrication_version  {self.fabrication_version}",
            "",
            "  win share",
        ]
        lines += [
            f"    {a:<6} {n:>4}  {n / total_wins:>5.0%}" for a, n in sorted(wins.items())
        ]
        lines += ["", "  action mix"]
        lines += [
            f"    {a:<22} {n:>5}  {n / len(self.transitions):>5.0%}"
            for a, n in sorted(actions.items(), key=lambda kv: -kv[1])
        ]
        return "\n".join(lines)

    def write_jsonl(self, path: str | Path) -> Path:
        """Transitions as JSONL, with a versions header line.

        The header is first so a reader hits it before any data: a file whose provenance is
        recorded only in a filename loses it the moment somebody renames the file.
        """
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "_header": True,
                "caps_version": self.caps_version,
                "encoder_version": self.encoder_version,
                "fabrication_version": self.fabrication_version,
                "seed": self.seed,
                "auctions": self.auctions,
                "completeness": self.completeness,
            }) + "\n")
            for transition in self.transitions:
                fh.write(json.dumps(transition.as_dict()) + "\n")
        return out


def generate(
    config: Config,
    seed: int = 0,
    shifts: int = 4,
    policy: BiddingPolicy | None = None,
    fab: FabricationRegister = DEFAULT,
    start: datetime | None = None,
    encoder=None,
) -> Dataset:
    """Run ``shifts`` eight-hour shifts and return the scored result.

    The loop, one bed release at a time::

        advance the world, admit whoever arrived
        pick the bidders whose standing allows it
        run the real allocation pipeline
        place the winner; hold the bed
        roll every participant forward to the horizon and score the outcome
        build shift-level episodes

    Budgets carry across auctions and roll at shift boundaries, because the objective is the
    discounted return *over a shift* and a per-auction budget would make pacing meaningless.
    """
    from allocation.rl.encoder import StateEncoder  # local: avoids an import cycle

    encoder = encoder or StateEncoder()
    world = SimWorld(seed=seed, fab=fab, start=start or datetime(2026, 8, 7, 7, 0, tzinfo=timezone.utc))
    source = SimDataSource(world)
    outcome_rng = Random(seed * 104729 + 5)

    hours = shifts * 8.0
    releases = world.release_schedule(hours)

    ledger = ParticipationLedger.for_candidates(config, (), world.start)
    budgets: dict[AgentKind, BudgetState] = {}
    bases = derive_all(config, (AgentKind.ER, AgentKind.OT, AgentKind.WARD))
    shift = resolve_shift(config, world.start)
    last_at = world.start

    transitions: list[Transition] = []
    steps: dict[tuple[AgentKind, str], list[Step]] = {}
    auctions = 0

    for moment in releases:
        # Arrivals BEFORE the clock moves. `arrivals_until` samples over `(world.now, moment]`,
        # so advancing first collapses that window to zero and nobody is ever admitted.
        for patient in world.arrivals_until(moment):
            ledger.admit(patient.candidate, moment)
        world.advance_to(moment)
        world.discharge("icu")  # the release event: a bed physically frees

        current = resolve_shift(config, moment)
        if current.shift_id != shift.shift_id or not budgets:
            occupancy = world.state("icu").occupancy
            budgets = {
                agent: open_shift(
                    config, bases[agent],
                    compute_factors(config, agent, occupancy_4h=occupancy),
                    current,
                )
                for agent in bases
            }
            shift, last_at = current, current.start

        elapsed = max(0.0, (moment - last_at).total_seconds() / 3600.0)
        if elapsed > 0:
            budgets = {a: recover(config, s, elapsed) for a, s in budgets.items()}
        last_at = moment

        pool = [p.candidate for p in world.active()]
        bidders = ledger.bidders(pool, moment)
        # One candidate per department: the auction has one Position per agent, and a
        # department fielding two patients at once is a queueing question this profile does
        # not model. The sickest goes forward, which is the ordering `world.active` already
        # applies.
        chosen: dict[AgentKind, object] = {}
        for candidate in bidders:
            chosen.setdefault(candidate.agent, candidate)
        if len(chosen) < 2:
            continue  # an uncontested bed is not an auction worth learning from

        run = run_allocation(
            config=config, source=source, candidates=tuple(chosen.values()),
            now=moment, query="ICU bed", mode=AuctionMode.SIMULATION,
            budgets=budgets, charge_budgets=True, read_alternatives=True,
            policy=policy, resource_id=f"icu-{moment:%Y%m%d-%H%M}",
        )
        auctions += 1
        budgets = {**budgets, **run.outcome.budgets}
        ledger.record(run.outcome.result, moment)

        result = run.outcome.result
        if result.winner is not None and result.winning_candidate_id:
            world.place(result.winning_candidate_id, "icu", moment)

        fates = _fates(world, result, config, fab, outcome_rng)

        # PER-AGENT credit (F-23). One shared scalar handed to every bidder made this a
        # free-rider game: identical reward whether you won or lost, and winning costs budget.
        # A policy trained on it learned to stop competing, which was correct play against a
        # wrong reward. `reward.yaml` tags every term `scenario: won` / `scenario: lost` —
        # those are the two perspectives, and this scores each agent from its own.
        rows = {
            fate.agent: score_for_agent(
                config, result.auction_id, fate, fates, fab, outcome_rng,
                observed_at=result.closed_at + timedelta(hours=4),
            )
            for fate in fates
        }

        for agent, position in result.positions.items():
            bids = result.bids_for(agent)
            if not bids:
                continue
            final = bids[-1]
            spend = run.outcome.spends.get(agent)
            won = result.winner is agent
            budget = budgets[agent]
            row = rows.get(agent)
            if row is None:
                continue
            transitions.append(
                Transition(
                    auction_id=result.auction_id, shift_id=shift.shift_id, agent=agent,
                    candidate_id=position.candidate_id,
                    state=encoder.encode(
                        agent=agent, utility=final.utility, ceiling=final.ceiling,
                        budget=budget, result=result, snapshot=run.snapshot,
                        options=run.pathways.get(agent),
                    ),
                    q_action=final.q_action or QAction.CONTINUE,
                    alpha=final.alpha, won=won, bid=position.current_bid,
                    utility=final.utility, ceiling=final.ceiling,
                    cost=spend.cost if spend else 0.0,
                    reward=row.reward_total, complete=row.complete,
                    feasible=tuple(sorted(a.value for a in final.feasible)),
                    budget_remaining=budget.budget_remaining,
                    burn_rate=budget.burn_rate,
                )
            )
            steps.setdefault((agent, shift.shift_id), []).append(
                Step(
                    auction_id=result.auction_id, round_count=result.rounds_run, won=won,
                    bid=position.current_bid, utility=final.utility,
                    cost=spend.cost if spend else 0.0,
                    reward=row.reward_total, complete=row.complete,
                )
            )

    transitions = _link_trajectories(transitions)

    episodes = tuple(
        build_episode(config, agent, shift_id, agent_steps)
        for (agent, shift_id), agent_steps in sorted(
            steps.items(), key=lambda kv: (kv[0][1], kv[0][0].value)
        )
    )

    return Dataset(
        episodes=episodes, transitions=tuple(transitions),
        caps_version=config.caps_version, encoder_version=encoder.version,
        fabrication_version=fab.version, seed=seed, auctions=auctions,
        abandonments=ledger.abandoned,
    )


def _fates(
    world: SimWorld, result, config: Config, fab: FabricationRegister, rng: Random
) -> tuple[Fate, ...]:
    """Roll every participant to the horizon and record what became of them.

    Losers are resolved too. §24 is not an afterthought — *"both episodes are needed"* — and a
    reward computed only from the winner cannot teach a policy that losing was survivable.
    """
    horizon = float(config.reward["horizon_hours"])
    out: list[Fate] = []
    for agent, position in result.positions.items():
        patient = world.patients.get(position.candidate_id)
        if patient is None:
            continue
        bids = result.bids_for(agent)
        exit_action = None
        for bid in reversed(bids):
            if bid.q_action is not None and bid.q_action.exits:
                exit_action = bid.q_action
                break
        # A patient who withdrew to an alternative is in that unit for the window, and
        # recovering at the lesser rate — which is exactly what the alternative bought.
        if exit_action is QAction.WITHDRAW_ALTERNATIVE and not patient.placed:
            plan = next((b.plan for b in reversed(bids) if b.plan), None)
            if plan and plan.target_unit:
                world.patients[position.candidate_id] = patient.placed_in(
                    plan.target_unit, result.closed_at
                )
                patient = world.patients[position.candidate_id]
        out.append(resolve(patient, horizon, fab, rng, exit_action=exit_action))
    return tuple(out)


def _link_trajectories(transitions: Sequence[Transition]) -> tuple[Transition, ...]:
    """Chain each agent's auctions within a shift into a trajectory.

    **This is what turns a pile of labelled decisions into an MDP.** Each transition gets the
    state its agent faced at its *next* auction in the same shift, and the last one in each
    shift is marked terminal.

    The episode boundary is the shift and not the auction, because the shift is where the budget
    lives. RL-Steps section 21: *"the agent does not maximize R(current auction). It tries to
    maximize the sum of discounted rewards over 8 hours."* An auction-level MDP would be a
    bandit — every decision independent, no consequence to spending — and the entire pacing
    problem the budget exists to create would be invisible to the learner.

    Crossing a shift boundary would be worse than leaving it terminal: budgets are reset and
    recomputed at the roll, so a bootstrap across it would credit a 07:00 decision with a return
    earned out of an allowance that decision never spent.
    """
    from collections import defaultdict

    chains: dict[tuple, list[int]] = defaultdict(list)
    for index, transition in enumerate(transitions):
        chains[(transition.agent, transition.shift_id)].append(index)

    linked = list(transitions)
    for indices in chains.values():
        for position, index in enumerate(indices):
            if position + 1 < len(indices):
                nxt = transitions[indices[position + 1]]
                linked[index] = replace(
                    linked[index],
                    next_state=nxt.state,
                    next_feasible=nxt.feasible,
                    terminal=False,
                )
            else:
                linked[index] = replace(linked[index], terminal=True)
    return tuple(linked)
