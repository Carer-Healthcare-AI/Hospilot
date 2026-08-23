"""The audit layer — the log everything else waits on.

These tests are mostly about **refusing to write**. The failure mode this layer exists to
prevent is not a crash; it is a log that looks fine for a year and then cannot answer the
question it was collected for, because nothing in it backfills.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from allocation.audit import (
    DuplicateAuction,
    IncompleteAuditRecord,
    InMemorySink,
    JsonlSink,
    build_bundle,
    build_outcome_row,
    statements,
    violations,
)
from allocation.auction import run_auction
from allocation.budget import derive_base
from allocation.contracts import AgentKind, AuctionMode
from allocation.ingest.fixtures import NOW
from allocation.policy import HeuristicPolicy
from tests.test_auction import (
    CANDIDATES,
    SECTION_19_BUDGETS,
    Section18Utilities,
    _budget,
)


@pytest.fixture
def event():
    from datetime import datetime, timezone

    from allocation.contracts import ReleaseEvent, ResourceType, TriggerSource

    return ReleaseEvent(
        event_id="evt-1",
        resource_type=ResourceType.ICU_BED,
        resource_id="icu-bed-07",
        predicted_free_at=datetime(2026, 8, 7, 13, 30, tzinfo=timezone.utc),
        detected_at=NOW,
        source=TriggerSource.DISCHARGE_PREDICTION,
        mode=AuctionMode.LIVE,
    )


@pytest.fixture
def outcome(config, profile, snapshot, event):
    budgets = {a: _budget(config, a, t) for a, t in SECTION_19_BUDGETS.items()}
    return run_auction(
        config, profile, event, CANDIDATES, Section18Utilities(config),
        HeuristicPolicy(config), budgets, snapshot,
    )


@pytest.fixture
def bundle(outcome, event, snapshot, config):
    bases = {a: derive_base(config, a) for a in (AgentKind.ER, AgentKind.OT, AgentKind.WARD)}
    return build_bundle(outcome, event, snapshot, CANDIDATES, bases=bases)


# ---------------------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------------------


def test_every_agent_every_round_is_recorded(bundle):
    """A log of winners answers no question worth asking."""
    by_round: dict[int, set[str]] = {}
    for bid in bundle.bids:
        by_round.setdefault(bid.round_index, set()).add(bid.agent)

    assert by_round[0] == {"er", "ot", "ward"}
    assert by_round[1] == {"er", "ot", "ward"}, "Ward's withdrawal is a row, not an omission"
    assert "ot" in by_round[2], "OT's withdrawal in round 3 is recorded too"


def test_withdrawals_keep_their_utility_and_bid(bundle):
    """Fairness v3 weights a loss by the utility forgone, so a withdrawal must carry both."""
    ward = next(b for b in bundle.bids if b.agent == "ward" and b.action == "withdraw")
    assert ward.amount == 55.0
    assert ward.utility == 76.0
    assert ward.component_points, "and its component breakdown"


def test_component_breakdown_is_stored_not_just_the_total(bundle):
    """B.13 needs to know what made up a utility, not what it summed to."""
    for bid in bundle.bids:
        assert bid.component_points, f"{bid.agent} round {bid.round_index}"
        assert bid.component_coverage
        assert sum(bid.component_points.values()) == pytest.approx(bid.utility)


def test_versions_are_on_every_row(bundle):
    """A row with no caps_version cannot be re-derived after a cap change."""
    assert bundle.auction.caps_version and bundle.auction.config_version
    for budget in bundle.budgets:
        assert budget.caps_version == bundle.auction.caps_version
    for snap in bundle.snapshots:
        assert snap.caps_version == bundle.auction.caps_version


def test_budget_rows_keep_all_four_factors_and_the_base_inputs(bundle):
    """Storing the product alone makes a budget unauditable (AGENT_BUDGET section 10)."""
    row = next(b for b in bundle.budgets if b.agent == "er")
    assert row.demand_factor and row.fairness_factor and row.scarcity_factor
    assert row.n_win == 4 and row.n_req == 6
    assert row.cost_per_win and row.cost_per_loss


def test_cost_is_attached_once_not_every_round(bundle):
    """An agent charged in all three rounds looks like it paid three times for one bed."""
    er_costs = [b.cost for b in bundle.bids if b.agent == "er" and b.cost is not None]
    assert len(er_costs) == 1
    assert er_costs[0] == pytest.approx(34.9, abs=0.5)


def test_unsigned_rules_travel_with_the_auction(bundle):
    """No stored auction is silently built on assumed clinical values."""
    assert "unit_benefit" in bundle.auction.unsigned_rules
    assert bundle.auction.unsigned_rules["budget.targets"] == "assumed_pending_governance"


def test_snapshot_preserves_absence_as_absence(engine, snapshot):
    """Absent must survive the round trip. Serialising it as 0.0 would be a silent lie.

    Scored through the real engine rather than section 18's injected utilities, because the
    claim is about the serialiser and the factors behind a score.
    """
    from allocation.audit.serialise import factor_signals
    from allocation.ingest.fixtures import ER_CANDIDATE

    breakdown = engine.score(ER_CANDIDATE, snapshot, snapshot.taken_at)
    signals = factor_signals({ER_CANDIDATE.candidate_id: breakdown})
    components = signals["ER-Patient-A"]["components"]

    age = components["clinical_benefit"]["factors"]["age_comorbidity"]
    assert age["value"] is None, "no DOB in hospilot.patients — absent, not zero"
    assert "DOB" in age["note"], "and the reason travels with it"

    ttc = components["urgency"]["factors"]["time_to_critical"]
    assert ttc["value"] is None and "B.5" in ttc["note"]

    present = components["clinical_benefit"]["factors"]["organ_risk"]
    assert present["value"] == pytest.approx(0.58, abs=0.01)
    assert present["source"] == "lab_results"


def test_participants_include_anyone_who_never_bid(bundle):
    """B.10 needs denials. An eligible candidate that never bid leaves no other trace."""
    assert set(bundle.auction.participants) == {"er", "ot", "ward"}
    assert bundle.auction.participants["ward"] == "Ward-Patient-C"


# ---------------------------------------------------------------------------------------
# What gets refused
# ---------------------------------------------------------------------------------------


def test_missing_component_breakdown_is_refused(bundle):
    stripped = replace(
        bundle,
        bids=tuple(replace(b, component_points={}) for b in bundle.bids),
    )
    problems = violations(stripped)
    assert any("B.13" in p for p in problems)


def test_dropping_the_losers_is_refused(bundle):
    winners_only = replace(bundle, bids=tuple(b for b in bundle.bids if b.agent == "er"))
    problems = violations(winners_only)
    assert any("every participant" in p for p in problems)


def test_missing_versions_are_refused(bundle):
    unversioned = replace(bundle, auction=replace(bundle.auction, caps_version=""))
    assert any("caps_version" in p for p in violations(unversioned))


def test_missing_snapshots_are_refused(bundle):
    assert any("re-derived" in p for p in violations(replace(bundle, snapshots=())))


def test_missing_policy_name_is_refused(bundle):
    anonymous = replace(bundle, bids=tuple(replace(b, policy_name="") for b in bundle.bids))
    assert any("policy_name" in p for p in violations(anonymous))


def test_a_bid_above_its_ceiling_is_refused(bundle):
    broken = replace(
        bundle,
        bids=tuple(
            replace(b, amount=b.ceiling + 10) if b.action != "withdraw" else b
            for b in bundle.bids
        ),
    )
    assert any("exceeds its own ceiling" in p for p in violations(broken))


def test_build_bundle_validates_by_default(outcome, event, snapshot):
    """The cost of a failed write is an alert; the cost of a bad write is found years later."""
    empty = replace(outcome.result, rounds=(), breakdowns=())
    with pytest.raises(IncompleteAuditRecord):
        build_bundle(replace(outcome, result=empty), event, snapshot, CANDIDATES)


def test_a_clean_bundle_has_no_violations(bundle):
    assert violations(bundle) == ()


# ---------------------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------------------


def test_live_auctions_are_idempotent_on_the_auction_key(bundle):
    """Section 7 — a re-firing prediction must not open two auctions on one bed."""
    sink = InMemorySink()
    sink.write(bundle)
    with pytest.raises(DuplicateAuction):
        sink.write(bundle)


def test_simulation_runs_never_collide_with_a_live_auction(bundle):
    """Deliberately exempt, so testing cannot block a real allocation."""
    sink = InMemorySink()
    sim = replace(bundle, auction=replace(bundle.auction, mode="simulation"))
    sink.write(sim)
    sink.write(sim)
    sink.write(bundle)
    assert len(sink.bundles) == 3
    assert len(sink.training_bundles()) == 1, "only the live one is training data"


def test_jsonl_round_trip(bundle, tmp_path):
    """Portable training data with no database — what the simulator will write."""
    sink = JsonlSink(tmp_path / "auctions.jsonl")
    sink.write(bundle)

    rows = sink.read()
    tables = {row["_table"] for row in rows}
    assert tables == {"auction", "auction_bid", "agent_budget", "utility_snapshot"}
    assert len(rows) == bundle.row_count
    assert json.dumps(rows), "every row must be JSON-serialisable"


def test_sql_statements_are_parameterised_and_ordered(bundle):
    """No driver is imported and no string is interpolated."""
    stmts = statements(bundle)
    assert stmts[0][0].startswith("INSERT INTO allocation.auction ")
    assert len(stmts) == bundle.row_count
    for sql, params in stmts:
        assert sql.count("%s") == len(params), sql[:60]


def test_postgres_sink_wraps_everything_in_one_transaction(bundle):
    """The budget decrement and the bid rows land together or not at all."""
    from allocation.audit import PostgresSink

    executed: list[str] = []
    sink = PostgresSink(lambda sql, params: executed.append(sql.split()[0]))
    sink.write(bundle)
    assert executed[0] == "BEGIN"
    assert executed[-1] == "COMMIT"
    assert executed.count("INSERT") == bundle.row_count


def test_a_failed_write_rolls_back(bundle):
    from allocation.audit import PostgresSink

    executed: list[str] = []

    def executor(sql: str, params: tuple) -> None:
        executed.append(sql.split()[0])
        if sql.startswith("INSERT INTO allocation.auction_bid"):
            raise RuntimeError("connection lost")

    with pytest.raises(RuntimeError):
        PostgresSink(executor).write(bundle)
    assert executed[-1] == "ROLLBACK"
    assert "COMMIT" not in executed


# ---------------------------------------------------------------------------------------
# Outcomes — F-01
# ---------------------------------------------------------------------------------------


def test_unknown_mortality_marks_the_episode_incomplete(bundle):
    """F-01. There is no deceased/expired/death column anywhere in the hospilot schema.

    ``None`` means *not known* and must never be read as "no death occurred". The episode is
    flagged incomplete, which is what should keep it out of a training set — the term is
    +30/-60 and it sets the sign of the whole episode.
    """
    row = build_outcome_row(
        bundle.auction_id,
        terms={"transferred_to_icu": 50, "patient_stabilised": 40, "boarding_reduced": 15},
        horizon_hours=4.0,
    )
    assert row.mortality_observed is None
    assert row.complete is False
    assert "no_mortality" in row.missing_terms
    assert row.reward_total == 105


def test_a_complete_outcome_needs_a_mortality_source(bundle):
    """Once a disposition field exists, the episode becomes usable."""
    row = build_outcome_row(
        bundle.auction_id,
        terms={"transferred_to_icu": 50, "no_mortality": 30},
        horizon_hours=4.0,
        mortality_observed=False,
        mortality_source="ipd_admissions.disposition",
    )
    assert row.complete is True
    assert row.missing_terms == ()
    assert row.reward_total == 80


def test_outcome_is_recorded_against_its_auction(bundle):
    sink = InMemorySink()
    sink.write(bundle)
    sink.record_outcome(build_outcome_row(bundle.auction_id, {"x": 1.0}, 4.0))
    assert sink.outcomes[0].auction_id == bundle.auction_id
