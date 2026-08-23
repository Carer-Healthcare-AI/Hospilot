"""The end-to-end path: query in, validated audit bundle out.

Every layer below is tested on its own. What is tested here is only what emerges from
*sequencing* them — the invariants that no single layer can check because each sees one piece.
"""

from __future__ import annotations

import json

import pytest

from allocation.cli import DEFAULT_QUERY, main, render, to_json
from allocation.contracts import Action, AgentKind, AuctionMode
from allocation.ingest.fixtures import CANDIDATES, NOW, FixtureDataSource
from allocation.trigger import UnknownUseCase, resolve_profile, run_allocation
from allocation.trigger.query import manual_event


@pytest.fixture
def run(config):
    return run_allocation(
        config=config,
        source=FixtureDataSource(),
        candidates=CANDIDATES,
        now=NOW,
        query=DEFAULT_QUERY,
    )


# -- use-case resolution ------------------------------------------------------------------


def test_the_framework_query_selects_the_icu_bed_profile():
    assert resolve_profile(DEFAULT_QUERY).resource_type.value == "icu_bed"


@pytest.mark.parametrize(
    "query",
    [
        "one limited ICU bed",
        "Who should get the ICU bed?",
        "ICU bed allocation",
        "we have two ICU beds free",
        "which department gets the critical care bed",
        "intensive care bed needed",
        "intensive care unit bed",
        # punctuation — a hyphen is not a distinction anyone typing a query intends
        "allocate the last ICU-bed",
        "critical-care bed",
        "icu_bed",
        "one limited I.C.U. bed",
        "ICU  bed with extra spaces",
        # word order — the same resource, named the other way round
        "a bed in the ICU",
        "Should ER or OT get the bed in intensive care?",
        # UK usage
        "ITU bed",
    ],
)
def test_the_same_resource_resolves_however_it_is_phrased(query):
    """An exact-phrase list only ever matches the word order someone wrote down.

    Every one of these names one ICU bed. Rejecting any of them is an obstacle, not a safety
    property — the safety property is the next test.
    """
    assert resolve_profile(query).resource_type.value == "icu_bed"


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   ",
        "allocate a ventilator",
        "who should get the next dialysis chair?",
        # A unit with no thing, and a thing with no unit, are both incomplete.
        "the ICU is full",
        "find me a bed",
        # Two resources named equally closely. Resolving either would auction a bed nobody
        # asked about; the caller has to say which.
        "we have an ICU bed and a ward bed free",
    ],
)
def test_a_query_that_does_not_name_one_resource_raises(query):
    """There is deliberately no default.

    Scoring one resource with another's caps produces a plausible number that is valid for
    nothing, and nothing downstream could detect it.
    """
    with pytest.raises(UnknownUseCase):
        resolve_profile(query)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("HDU bed", "hdu_bed"),
        ("high dependency bed", "hdu_bed"),
        ("a step down bed is opening up", "hdu_bed"),
        ("is there a PACU bed free", "pacu_bed"),
        ("recovery bay available", "pacu_bed"),
        ("who gets the ward bed", "ward_bed"),
        ("general ward bed allocation", "ward_bed"),
        ("resus bay free", "resus_bed"),
        ("ED cubicle open", "ed_bed"),
        ("emergency department bed", "ed_bed"),
    ],
)
def test_every_bed_in_the_family_resolves_to_its_own_profile(query, expected):
    """These used to raise, and the inversion is the point of the bed family.

    HDU and PACU were excluded because they are the *alternative* pathways that half the
    withdrawals in RL-Steps turn on. They still are — Alternative Availability still scores
    them — but a unit being a fallback for one auction does not stop it being the resource of
    another. What must never happen is an HDU query resolving to an *ICU* auction, and that is
    now prevented by HDU having its own profile rather than by refusing to answer.
    """
    assert resolve_profile(query).resource_type.value == expected


def test_a_bidder_named_in_the_query_is_not_mistaken_for_the_resource():
    """The founding sentence names Ward as a *bidder* and ICU as the *resource*.

    "ER, OT, and ICU/Ward demand compete for one limited ICU bed" contains both unit words, so
    on presence alone it names two resources. Token gap separates them: `icu` sits against
    `bed`, `ward` qualifies `demand`. Refusing this sentence would refuse the one query the
    engine was built to answer.
    """
    assert resolve_profile(DEFAULT_QUERY).resource_type.value == "icu_bed"
    assert resolve_profile("ward and ICU demand compete for one ICU bed").resource_type.value == (
        "icu_bed"
    )
    assert resolve_profile("ER and ICU teams want the ward bed").resource_type.value == "ward_bed"


def test_the_resolution_is_echoed_so_a_wrong_match_is_visible(run):
    """The matcher is permissive by design, so how it resolved has to be on screen."""
    assert "matched on" in dict(run.trace["use_case"].rows)


# -- the safety property of a hand-fired run ----------------------------------------------


def test_a_typed_query_is_never_binding():
    profile = resolve_profile(DEFAULT_QUERY)
    assert manual_event(profile, NOW).mode is AuctionMode.SIMULATION


def test_a_hand_fired_events_resource_id_names_its_own_resource():
    """It read ``"icu-bed-manual"`` for every resource, ICU-shaped like the occupancy was.

    ``resource_id`` is the column identifying *which* bed was at stake, and it is persisted, so
    a ward-bed auction logged under an ICU bed's id is an audit row that misidentifies its own
    subject. An explicit id still wins — a real release event carries the bed's real id.
    """
    from allocation.contracts import ResourceType
    from allocation.profiles.registry import REGISTRY

    for resource_type in ResourceType:
        event = manual_event(REGISTRY.get(resource_type), NOW)
        assert event.resource_id == f"{resource_type.value}-manual"
        assert event.resource_type is resource_type

    ward = REGISTRY.get(ResourceType.WARD_BED)
    assert manual_event(ward, NOW, resource_id="bed-4021").resource_id == "bed-4021"


def test_simulation_computes_costs_but_charges_nothing(run):
    """Scored identically to a live run, but no budget moves.

    Both halves matter: a shadow run that skipped the arithmetic would not be comparable to
    a live one, and one that charged would corrupt a real budget from a test.
    """
    assert run.outcome.spends, "costs must still be computed"
    assert all(spend.cost > 0 for spend in run.outcome.spends.values())
    assert all(state.spent == 0.0 for state in run.outcome.budgets.values())


def test_cli_refuses_live_against_fixture_patients(capsys):
    assert main(["--mode", "live"]) == 2
    assert "do not exist" in capsys.readouterr().err


# -- what only the whole sequence can show ------------------------------------------------


def test_bid_utility_and_ceiling_stay_three_different_things(run):
    """The load-bearing invariant, checked on the winner of a real end-to-end run.

    Note what is NOT asserted: that utility exceeds the bid. With the B.9 uplift active the
    ceiling sits above utility, so a bid may legitimately pass it — that is RL-Steps' own Ward
    case, valuing the bed at 86 and willing to spend 105. The invariant is that all three are
    distinct and that the bid never reaches the ceiling, not that they are ordered.
    """
    winner = run.outcome.result.winner
    position = run.outcome.result.positions[winner]

    assert position.current_bid < position.ceiling, "won without exhausting willingness to pay"
    assert position.utility != position.ceiling, "the uplift should separate these"
    assert position.current_bid != position.utility


def test_no_bid_ever_exceeds_its_ceiling(run):
    for round_state in run.outcome.result.rounds:
        for bid in round_state.bids:
            assert bid.amount <= bid.ceiling + 1e-9, (
                f"{bid.agent.value} bid {bid.amount} above ceiling {bid.ceiling} "
                f"in round {bid.round_index}"
            )


def test_the_highest_utility_agent_wins_on_appendix_c(run):
    """RL-Steps section 18 awards the bed to ER, and so does the engine.

    The bid *ladder* does not match the document — these utilities are computed from
    Appendix C (107.4 / 35.1 / 48.6) rather than RL-Steps' published 135 / 117 / 86 (F-20).
    The ranking is what survives that discrepancy, and the ranking is what allocates the bed.
    """
    assert run.winner is AgentKind.ER

    totals = {run.outcome.result.positions[a].agent: b.total for a, b in
              zip([c.agent for c in CANDIDATES], run.utilities.values())}
    assert max(totals, key=totals.get) is AgentKind.ER


def test_every_round_of_every_agent_is_logged_including_losers(run):
    """Sections 23-24: a losing episode is training data too."""
    agents_in_round_zero = {bid.agent for bid in run.outcome.result.rounds[0].bids}
    assert agents_in_round_zero == {AgentKind.ER, AgentKind.OT, AgentKind.WARD}

    withdrawn = {
        bid.agent
        for round_state in run.outcome.result.rounds
        for bid in round_state.bids
        if bid.action is Action.WITHDRAW
    }
    assert withdrawn, "the fixture should produce withdrawals"
    logged = {row.agent for row in run.bundle.bids}
    assert {a.value for a in withdrawn} <= logged


def test_the_first_round_is_sealed(run):
    """RL-Steps section 10: 'current highest competition unknown'.

    If round 0 were open, the later bidders would price against the earlier ones' bids. The
    check is that every opening bid follows from the agent's own ceiling alone.
    """
    opening = run.outcome.result.rounds[0]
    alphas = {bid.agent: bid.alpha for bid in opening.bids if bid.alpha is not None}
    assert len(alphas) == 3
    for bid in opening.bids:
        if bid.alpha is not None:
            # Opening from a standing bid of 0, so headroom is the whole ceiling. Bids are
            # whole points (rounds.py) — the check is that nothing but the agent's own
            # ceiling and alpha entered the number.
            assert bid.amount == float(round(bid.alpha * bid.ceiling))


def test_versions_are_stamped_on_every_row(run):
    """A score that cannot be tied to the caps that produced it cannot be re-derived."""
    result = run.outcome.result
    assert result.caps_version and result.config_version
    assert all(row.caps_version == result.caps_version for row in run.bundle.budgets)
    assert all(row.caps_version == result.caps_version for row in run.bundle.snapshots)


def test_the_reward_is_scheduled_not_scored(run):
    """The episode exists in an unscored state for hours. F-01 means it never completes."""
    assert run.pending.due_at > run.outcome.result.closed_at
    assert not run.pending.is_due(run.outcome.result.closed_at)
    assert run.pending.is_due(run.pending.due_at)


def test_utilities_are_rescored_each_round(run):
    """Section 13 has ER's utility rising between rounds; the engine must re-read, not cache.

    Against a fixture the movement is small — only the time-dependent factors change — but it
    must not be zero, or the auction is scoring a world frozen at round 1.
    """
    breakdowns = run.outcome.result.breakdowns
    assert len(breakdowns) >= 2
    first = breakdowns[0]["ER-Patient-A"].total
    last = breakdowns[-1]["ER-Patient-A"].total
    assert last != first


# -- the trace is the thing that makes a run checkable ------------------------------------


def test_the_trace_covers_every_stage(run):
    keys = [step.key for step in run.trace]
    for expected in ("use_case", "ingest", "utility", "ceiling", "budget", "round_budget",
                     "close", "audit", "reward"):
        assert expected in keys


def test_the_trace_agrees_with_the_document_where_it_publishes_a_value(run):
    assert run.trace.checked, "nothing was checked against the framework"
    assert run.trace.agrees, f"disagreements: {[s.disagreements for s in run.trace.failures]}"


def test_every_step_names_the_section_it_implements(run):
    assert all(step.section for step in run.trace)


# -- the CLI --------------------------------------------------------------------------


def test_cli_runs_and_reports_a_winner(capsys):
    assert main([]) == 0
    assert "RESULT" in capsys.readouterr().out


def test_cli_json_is_valid_and_carries_the_versions(run):
    payload = json.loads(to_json(run))
    assert payload["winner"] == "er"
    assert payload["caps_version"] and payload["config_version"]
    assert payload["binding"] is False
    assert len(payload["steps"]) == len(run.trace)


def test_cli_rejects_an_unknown_query(capsys):
    assert main(["allocate an ambulance"]) == 1
    assert "cannot resolve" in capsys.readouterr().err


def test_render_marks_absent_values_as_absent(run):
    """Never as 0 — the three-state rule has to survive all the way to the screen."""
    text = render(run)
    assert "RESULT" in text
    assert "0 (absent)" not in text
