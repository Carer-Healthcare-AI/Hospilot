"""Complete mathematical trace for a diagnostic simulation.

All auction values come from the diagnostic runtime records.  The renderer never invokes the
policy or scoring engine, so asking for an explanation cannot change or re-run a decision.
"""

from __future__ import annotations

import json
from typing import Any

from allocation.config import Config
from allocation.use_cases.diagnostic_machine.reward import DiagnosticRewardObserver
from allocation.use_cases.diagnostic_machine.scenarios import Scenario
from allocation.use_cases.diagnostic_machine.simulator import SimulationResult


def as_data(result: SimulationResult, scenario: Scenario, config: Config) -> dict[str, Any]:
    transport = {
        arrival.request.request_id: arrival.request.transport_minutes
        for arrival in scenario.arrivals
    }
    rewards = DiagnosticRewardObserver().score_all(result.outcomes, transport)
    auctions: list[dict[str, Any]] = []
    for auction_index, auction in enumerate(result.auctions, start=1):
        rounds = []
        for round_ in auction.rounds:
            bids = []
            for bid in round_.bids:
                headroom = bid.ceiling - bid.previous_bid
                increment = None if bid.alpha is None else bid.alpha * headroom
                bids.append({
                    "agent": bid.agent.value,
                    "request_id": bid.request_id,
                    "pathway": bid.pathway.value,
                    "action": bid.action.value,
                    "feasible_actions": sorted(action.value for action in bid.feasible),
                    "components": [
                        {
                            "name": name,
                            "score": bid.component_scores[name],
                            "cap": bid.component_caps[name],
                            "points": bid.component_points[name],
                        }
                        for name in sorted(bid.component_scores)
                    ],
                    "utility": bid.utility,
                    "ceiling": bid.ceiling,
                    "previous_bid": bid.previous_bid,
                    "leader_bid": bid.leader_bid,
                    "headroom": headroom,
                    "alpha": bid.alpha,
                    "increment": increment,
                    "proposed_bid": bid.proposed_bid,
                    "affordable_limit": bid.affordable_limit,
                    "remaining_budget": bid.remaining_budget,
                    "final_bid": bid.amount,
                    "contention": bid.contention,
                    "clamped_by": bid.clamped_by,
                    "plan": _plan(bid.plan),
                })
            rounds.append({
                "round": round_.round_index + 1,
                "opened_at": round_.opened_at.isoformat(),
                "active_agents": sorted(agent.value for agent in round_.active_agents),
                "highest_bid": round_.highest_bid,
                "bids": bids,
            })

        settlements = []
        for agent, spend in sorted(auction.spends.items(), key=lambda item: item[0].value):
            opening = auction.opening_budgets[agent]
            closing = auction.closing_budgets[agent]
            settlements.append({
                "agent": agent.value,
                "won": spend.won,
                "bid": spend.bid,
                "contention": spend.contention,
                "outcome_factor": spend.outcome_factor,
                "commitment_rate": spend.commitment_rate,
                "cost": spend.cost,
                "opening_budget": opening.budget_remaining,
                "closing_budget": closing.budget_remaining,
                "recovered": closing.recovered - opening.recovered,
            })
        interval = auction.awarded_interval
        auctions.append({
            "auction": auction_index,
            "modality": auction.modality.value,
            "machine_id": auction.machine_id,
            "opened_at": auction.opened_at.isoformat(),
            "closed_at": auction.closed_at.isoformat(),
            "contention": auction.contention,
            "reserve_price": auction.reserve_price,
            "outcome": auction.outcome,
            "winner": auction.winner.value if auction.winner else None,
            "winning_request_id": auction.winning_request_id,
            "winning_bid": auction.winning_bid,
            "rounds": rounds,
            "settlement": settlements,
            "awarded_interval": None if interval is None else {
                "machine_id": interval.machine_id,
                "request_id": interval.request_id,
                "starts_at": interval.starts_at.isoformat(),
                "ends_at": interval.ends_at.isoformat(),
            },
            "caps_version": auction.caps_version,
            "config_version": auction.config_version,
            "unsigned_rules": dict(sorted(auction.unsigned_rules.items())),
        })

    outcomes = []
    for outcome in sorted(result.outcomes, key=lambda item: item.request_id):
        reward = rewards[outcome.request_id]
        outcomes.append({
            "request_id": outcome.request_id,
            "agent": outcome.agent.value,
            "fate": outcome.fate,
            "note": outcome.note,
            "reward_terms": dict(reward.terms()),
            "reward_total": reward.total,
        })
    return {
        "scenario": scenario.name,
        "modality": scenario.modality.value,
        "assumption": "simulation_only_unfitted",
        "auctions": auctions,
        "outcomes": outcomes,
        "metrics": {
            "requests": result.metrics.requests,
            "answered": result.metrics.answered,
            "timeliness": result.metrics.timeliness,
            "utilisation": result.metrics.utilisation,
            "constraint_violations": result.metrics.constraint_violations,
        },
    }


def as_json(result: SimulationResult, scenario: Scenario, config: Config) -> str:
    return json.dumps(as_data(result, scenario, config), indent=2, sort_keys=True)


def explain(result: SimulationResult, scenario: Scenario, config: Config) -> str:
    data = as_data(result, scenario, config)
    lines = [
        "DIAGNOSTIC MACHINE — COMPLETE MATHEMATICAL TRACE",
        f"scenario {scenario.name}   modality {scenario.modality.value}",
        "values marked simulation-only and unfitted",
    ]
    for auction in data["auctions"]:
        lines += ["", "=" * 86,
                  f"AUCTION {auction['auction']} — {auction['machine_id']} — {auction['outcome']}",
                  f"contention {auction['contention']:.4f}   reserve {auction['reserve_price']:.2f}"]
        for round_ in auction["rounds"]:
            lines += ["", f"  ROUND {round_['round']}   highest {round_['highest_bid']:.2f}"]
            for bid in round_["bids"]:
                lines += ["", f"    {bid['agent'].upper()} — {bid['request_id']}"]
                for component in bid["components"]:
                    lines.append(
                        f"      {component['name']:22} {component['cap']:7.2f} x "
                        f"{component['score']:.4f} = {component['points']:8.2f}"
                    )
                lines += [
                    f"      utility               sum(points) = {bid['utility']:.2f}",
                    f"      ceiling               {bid['ceiling']:.2f}",
                    f"      pathway/action         {bid['pathway']} / {bid['action']}",
                    f"      feasible               {', '.join(bid['feasible_actions']) or 'none'}",
                    f"      leader                 {bid['leader_bid']:.2f}",
                    f"      headroom               {bid['ceiling']:.2f} - "
                    f"{bid['previous_bid']:.2f} = {bid['headroom']:.2f}",
                ]
                if bid["alpha"] is not None:
                    lines += [
                        f"      increment              {bid['alpha']:.4f} x "
                        f"{bid['headroom']:.2f} = {bid['increment']:.2f}",
                        f"      proposed               {bid['previous_bid']:.2f} + "
                        f"{bid['increment']:.2f} = {bid['proposed_bid']:.2f}",
                        f"      affordability limit    {bid['affordable_limit']:.2f}",
                    ]
                lines.append(
                    f"      FINAL BID              {bid['final_bid']:.2f}"
                    + (f"  [{bid['clamped_by']}]" if bid["clamped_by"] else "")
                )
        lines += ["", f"  RESULT winner={auction['winner']} request={auction['winning_request_id']} "
                  f"bid={auction['winning_bid']}", "  SETTLEMENT"]
        for row in auction["settlement"]:
            lines.append(
                f"    {row['agent']:5} cost = {row['bid']:.2f} x {row['contention']:.4f} x "
                f"{row['outcome_factor']:.2f} x {row['commitment_rate']:.2f} = "
                f"{row['cost']:.2f}; budget {row['opening_budget']:.2f} -> "
                f"{row['closing_budget']:.2f}"
            )
        if auction["awarded_interval"]:
            interval = auction["awarded_interval"]
            lines.append(
                f"  INTERVAL {interval['machine_id']} {interval['starts_at']} -> "
                f"{interval['ends_at']}"
            )
    lines += ["", "REWARDS"]
    for outcome in data["outcomes"]:
        terms = " + ".join(f"{name}={value:.2f}" for name, value in outcome["reward_terms"].items())
        lines.append(
            f"  {outcome['request_id']:16} {outcome['fate']:12} {terms} = "
            f"{outcome['reward_total']:.2f}"
        )
    return "\n".join(lines)


def _plan(plan: object | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "alternative_modality": (
            plan.alternative_modality.value if plan.alternative_modality else None
        ),
        "alternative_yield": plan.alternative_yield,
        "expected_capacity_at": (
            plan.expected_capacity_at.isoformat() if plan.expected_capacity_at else None
        ),
        "capacity_probability": plan.capacity_probability,
        "reentry_condition": plan.reentry_condition,
        "note": plan.note,
    }


__all__ = ["as_data", "as_json", "explain"]
