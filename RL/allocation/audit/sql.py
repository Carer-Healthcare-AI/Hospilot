"""Parameterised statements for migration 091's tables.

No driver is imported here and no connection is opened: the module produces
``(sql, params)`` pairs and something else executes them. That keeps the whole package free of
the framework it will eventually run inside, and keeps the statements testable without a
database.

Column lists are written out in full rather than generated. A generated column list silently
follows a schema change; a written one breaks, which is what you want when the table underneath
a five-year training log moves.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from allocation.audit.records import AuctionRow, AuditBundle, BidRow, BudgetRow, OutcomeRow, SnapshotRow

Statement = tuple[str, tuple[Any, ...]]


def _json(value: Any) -> str:
    from allocation.audit.serialise import jsonable

    return json.dumps(jsonable(value))


AUCTION_SQL = """
INSERT INTO allocation.auction (
    id, auction_key, resource_type, resource_id, mode, trigger_source,
    predicted_free_at, opened_at, closed_at, max_rounds, rounds_run, reserve_price,
    winning_agent, winning_candidate_id, winning_bid, outcome,
    caps_version, config_version, unsigned_rules, participants
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
""".strip()

BID_SQL = """
INSERT INTO allocation.auction_bid (
    auction_id, round_index, agent, candidate_id, patient_token,
    action, amount, utility, ceiling, alpha,
    contention, outcome_factor, cost,
    component_points, component_coverage, policy_name, decided_at,
    q_action, plan, q_values, feasible_actions
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s)
""".strip()

BUDGET_SQL = """
INSERT INTO allocation.agent_budget (
    agent, shift_id, shift_start, shift_end,
    base, demand_factor, criticality_factor, fairness_factor, scarcity_factor,
    factor_sources,
    budget_total, budget_remaining, spent_this_shift, recovered_this_shift,
    source, n_win, n_req, cost_per_win, cost_per_loss,
    caps_version, config_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (agent, shift_id) DO UPDATE SET
    budget_remaining     = EXCLUDED.budget_remaining,
    spent_this_shift     = EXCLUDED.spent_this_shift,
    recovered_this_shift = EXCLUDED.recovered_this_shift,
    updated_at           = now()
""".strip()

SNAPSHOT_SQL = """
INSERT INTO allocation.utility_snapshot (
    auction_id, round_index, taken_at,
    hospital_state, patient_data, factor_signals,
    caps_version, config_version
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
""".strip()

OUTCOME_SQL = """
INSERT INTO allocation.auction_outcome (
    auction_id, observed_at, horizon_hours, terms, reward_total,
    mortality_observed, mortality_source, complete, missing_terms
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (auction_id) DO UPDATE SET
    observed_at        = EXCLUDED.observed_at,
    terms              = EXCLUDED.terms,
    reward_total       = EXCLUDED.reward_total,
    mortality_observed = EXCLUDED.mortality_observed,
    mortality_source   = EXCLUDED.mortality_source,
    complete           = EXCLUDED.complete,
    missing_terms      = EXCLUDED.missing_terms
""".strip()


def auction_statement(row: AuctionRow) -> Statement:
    return AUCTION_SQL, (
        row.id, row.auction_key, row.resource_type, row.resource_id, row.mode,
        row.trigger_source, row.predicted_free_at, row.opened_at, row.closed_at,
        row.max_rounds, row.rounds_run, row.reserve_price,
        row.winning_agent, row.winning_candidate_id, row.winning_bid, row.outcome,
        row.caps_version, row.config_version, _json(row.unsigned_rules),
        _json(row.participants),
    )


def bid_statement(row: BidRow) -> Statement:
    return BID_SQL, (
        row.auction_id, row.round_index, row.agent, row.candidate_id, row.patient_token,
        row.action, row.amount, row.utility, row.ceiling, row.alpha,
        row.contention, row.outcome_factor, row.cost,
        _json(row.component_points), _json(row.component_coverage),
        row.policy_name, row.decided_at,
        row.q_action, _json(row.plan), _json(row.q_values), list(row.feasible_actions),
    )


def budget_statement(row: BudgetRow) -> Statement:
    return BUDGET_SQL, (
        row.agent, row.shift_id, row.shift_start, row.shift_end,
        row.base, row.demand_factor, row.criticality_factor, row.fairness_factor,
        row.scarcity_factor, _json(row.factor_sources),
        row.budget_total, row.budget_remaining, row.spent_this_shift, row.recovered_this_shift,
        row.source, row.n_win, row.n_req, row.cost_per_win, row.cost_per_loss,
        row.caps_version, row.config_version,
    )


def snapshot_statement(row: SnapshotRow) -> Statement:
    return SNAPSHOT_SQL, (
        row.auction_id, row.round_index, row.taken_at,
        _json(row.hospital_state), _json(row.patient_data), _json(row.factor_signals),
        row.caps_version, row.config_version,
    )


def outcome_statement(row: OutcomeRow) -> Statement:
    return OUTCOME_SQL, (
        row.auction_id, row.observed_at, row.horizon_hours, _json(row.terms),
        row.reward_total, row.mortality_observed, row.mortality_source,
        row.complete, list(row.missing_terms),
    )


def statements(bundle: AuditBundle) -> Sequence[Statement]:
    """Every statement for one bundle, in dependency order.

    The auction row goes first because the bid, budget and snapshot rows reference it. All of
    them run inside one transaction — the budget decrement and the bid rows must land
    together or not at all.
    """
    out: list[Statement] = [auction_statement(bundle.auction)]
    out += [bid_statement(row) for row in bundle.bids]
    out += [budget_statement(row) for row in bundle.budgets]
    out += [snapshot_statement(row) for row in bundle.snapshots]
    return tuple(out)
