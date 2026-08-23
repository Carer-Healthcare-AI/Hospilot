"""Turning a finished auction into rows.

The one thing worth stating explicitly: **the budget decrement and the bid rows are the same
write.** AGENT_BUDGET section 10 — *"Decrement happens once, at settle, in the same
transaction that writes auction_bid."* Split them and a crash between the two leaves a budget
that was spent on an auction that never happened, or an auction whose cost was never charged.
Neither is recoverable from the log, because the log is the only record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Sequence

from allocation.audit import serialise
from allocation.audit.records import (
    AuctionRow,
    AuditBundle,
    BidRow,
    BudgetRow,
    OutcomeRow,
    SnapshotRow,
)
from allocation.audit.validate import ensure_writable
from allocation.auction.runner import AuctionOutcome
from allocation.budget.base import BaseBudget
from allocation.contracts import (
    Action,
    AgentKind,
    Candidate,
    FeatureSnapshot,
    ReleaseEvent,
)


def build_bundle(
    outcome: AuctionOutcome,
    event: ReleaseEvent,
    snapshot: FeatureSnapshot,
    candidates: Sequence[Candidate],
    policy_name: str = "heuristic",
    bases: Mapping[AgentKind, BaseBudget] | None = None,
    validate: bool = True,
) -> AuditBundle:
    """Assemble every row for one auction."""
    result = outcome.result
    tokens = {c.candidate_id: c.patient_token for c in candidates}

    auction = AuctionRow(
        id=result.auction_id,
        auction_key=result.auction_key,
        resource_type=result.resource_type.value,
        resource_id=result.resource_id,
        mode=result.mode.value,
        trigger_source=event.source.value,
        predicted_free_at=event.predicted_free_at,
        opened_at=result.opened_at,
        closed_at=result.closed_at,
        max_rounds=result.max_rounds,
        rounds_run=result.rounds_run,
        reserve_price=result.reserve_price,
        winning_agent=result.winner.value if result.winner else None,
        winning_candidate_id=result.winning_candidate_id,
        winning_bid=result.winning_bid,
        outcome=result.outcome,
        caps_version=result.caps_version,
        config_version=result.config_version,
        unsigned_rules=dict(result.unsigned_rules),
        participants={a.value: p.candidate_id for a, p in result.positions.items()},
    )

    bids: list[BidRow] = []
    for round_state in result.rounds:
        breakdowns = result.breakdowns[round_state.round_index]
        for bid in round_state.bids:
            utility = breakdowns[bid.candidate_id]
            spend = outcome.spends.get(bid.agent)
            # Cost belongs to the settling round only. Attaching it to every round would
            # make an agent look charged three times for one allocation.
            is_final = round_state.round_index == result.rounds_run - 1 or (
                bid.action is Action.WITHDRAW
            )
            bids.append(
                BidRow(
                    auction_id=result.auction_id,
                    round_index=round_state.round_index,
                    agent=bid.agent.value,
                    candidate_id=bid.candidate_id,
                    patient_token=tokens.get(bid.candidate_id),
                    action=bid.action.value,
                    amount=bid.amount,
                    utility=bid.utility,
                    ceiling=bid.ceiling,
                    alpha=bid.alpha,
                    contention=bid.contention,
                    outcome_factor=spend.outcome_factor if (spend and is_final) else None,
                    cost=spend.cost if (spend and is_final) else None,
                    component_points=serialise.component_points(utility),
                    component_coverage=serialise.component_coverage(utility),
                    policy_name=policy_name,
                    decided_at=round_state.opened_at,
                    q_action=bid.q_action.value if bid.q_action else None,
                    plan=serialise.pathway_plan(bid.plan),
                    q_values={k.value: v for k, v in bid.q_values.items()},
                    feasible_actions=tuple(sorted(a.value for a in bid.feasible)),
                )
            )

    budgets = tuple(
        _budget_row(state, bases.get(agent) if bases else None)
        for agent, state in outcome.budgets.items()
    )

    snapshots = tuple(
        SnapshotRow(
            auction_id=result.auction_id,
            round_index=index,
            taken_at=snapshot.taken_at,
            hospital_state=serialise.hospital_state(snapshot),
            patient_data=serialise.patient_data(snapshot),
            factor_signals=serialise.factor_signals(breakdown),
            caps_version=result.caps_version,
            config_version=result.config_version,
        )
        for index, breakdown in enumerate(result.breakdowns[: result.rounds_run])
    )

    bundle = AuditBundle(
        auction=auction, bids=tuple(bids), budgets=budgets, snapshots=snapshots
    )
    return ensure_writable(bundle) if validate else bundle


def _budget_row(state, base: BaseBudget | None) -> BudgetRow:
    return BudgetRow(
        agent=state.agent.value,
        shift_id=state.shift_id,
        shift_start=state.shift_start,
        shift_end=state.shift_end,
        base=state.base,
        demand_factor=state.demand,
        criticality_factor=state.criticality,
        fairness_factor=state.fairness,
        scarcity_factor=state.scarcity,
        factor_sources=dict(state.factor_sources),
        budget_total=state.budget_total,
        budget_remaining=state.budget_remaining,
        spent_this_shift=state.spent,
        recovered_this_shift=state.recovered,
        source=state.source.value,
        n_win=base.n_win if base else None,
        n_req=base.n_req if base else None,
        cost_per_win=base.cost_per_win if base else None,
        cost_per_loss=base.cost_per_loss if base else None,
        caps_version=state.caps_version,
        config_version=state.config_version,
    )


def build_outcome_row(
    auction_id: str,
    terms: Mapping[str, float],
    horizon_hours: float,
    mortality_observed: bool | None = None,
    mortality_source: str | None = None,
    observed_at: datetime | None = None,
) -> OutcomeRow:
    """The reward, observed after the outcome window.

    ``mortality_observed=None`` is the only honest value today: there is no deceased,
    expired or death column anywhere in the hospilot schema, and ``discharge_summaries``
    holds free text (F-01). The term is listed in ``missing_terms`` so an episode built on an
    incomplete reward is never mistaken for a complete one — and ``complete`` stays False,
    which is what should keep it out of a training set.
    """
    missing: list[str] = []
    if mortality_observed is None:
        missing.append("no_mortality")
    missing += [name for name, value in terms.items() if value is None]

    return OutcomeRow(
        auction_id=auction_id,
        observed_at=observed_at or datetime.now(timezone.utc),
        horizon_hours=horizon_hours,
        terms={k: v for k, v in terms.items() if v is not None},
        reward_total=sum(v for v in terms.values() if v is not None),
        mortality_observed=mortality_observed,
        mortality_source=mortality_source,
        complete=not missing,
        missing_terms=tuple(missing),
    )
