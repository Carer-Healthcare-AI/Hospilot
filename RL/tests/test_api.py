"""The HTTP surface. Every test here is about the wrapper, never the mechanism.

The auction is already covered by ``test_auction.py`` against RL-Steps section 18. What is
untested until now is the *translation*: that a JSON body reaches the same functions the CLI
reaches, that the answer is complete enough to check without re-running, and — the part with
teeth — that the CLI's escape hatches did not become remotely reachable when they grew a port.

Three properties are asserted here that exist nowhere else:

* ``mode: live`` is refused, so the fixture patients cannot reach a real bed over HTTP
* a scenario is addressed by **name**, so a request body cannot name a path
* HTTP and CLI agree on the same inputs — one implementation, not two
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="the HTTP API is an optional extra")

from fastapi.testclient import TestClient  # noqa: E402

from allocation.api import service  # noqa: E402
from allocation.api.app import create_app  # noqa: E402
from allocation.cli import main as cli_main  # noqa: E402

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(scenario_dir=SCENARIO_DIR))


# ---------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------


def test_health_reports_the_versions_it_is_running(client):
    """A caller comparing two responses needs to know whether the config moved under them."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["caps_version"] and body["config_version"]
    assert body["live_mode"] == "refused"


def test_config_surfaces_every_unsigned_rule(client):
    """The unsigned register is governance, not decoration — it must survive the API boundary.

    ``Config.unsigned`` exists so no run is silently built on assumed clinical values. An API
    that dropped it would let a caller consume a number without the one piece of context that
    says how much to trust it.
    """
    body = client.get("/config").json()
    assert body["unsigned_rules"], "the shipped config has unsigned tables; none were reported"
    assert "reward.terms" in body["unsigned_rules"]
    assert "auction.safety_constraints" in body["unsigned_rules"]
    assert body["safety_constraints_declared"] is False


def test_use_cases_offers_a_query_that_actually_resolves(client):
    """There is deliberately no default profile, so discovery has to be served, not guessed."""
    for profile in client.get("/use-cases").json()["profiles"]:
        answer = client.post("/auction", json={"query": profile["example_query"]})
        assert answer.status_code == 200, profile["example_query"]


def test_scenarios_lists_the_shipped_one(client):
    assert "ward_crash" in client.get("/scenarios").json()["scenarios"]


# ---------------------------------------------------------------------------------------
# Running one auction
# ---------------------------------------------------------------------------------------


def test_empty_body_runs_the_reference_auction(client):
    body = client.post("/auction", json={}).json()
    assert body["outcome"] == "awarded"
    assert body["winner"] == "er"
    assert body["rounds_run"] == 3
    assert body["binding"] is False


def test_the_bid_ladder_is_returned_not_just_the_winner(client):
    """Without the ladder a caller cannot check the result, only accept it.

    The whole content of an auction is how the price got there. ``cli.to_json`` reports the
    close, which is the right summary for a terminal; an API that did the same would hand back
    a number whose derivation requires re-running the auction — and a re-run re-reads the
    world, so it would not necessarily agree.
    """
    body = client.post("/auction", json={}).json()
    rounds = body["rounds"]
    assert len(rounds) == body["rounds_run"]

    er = [
        bid
        for state in rounds
        for bid in state["bids"]
        if bid["agent"] == "er" and bid["action"] == "increase_bid"
    ]
    assert [b["amount"] for b in er] == sorted(b["amount"] for b in er), "bids must ascend"
    assert er[-1]["amount"] == body["winning_bid"]
    assert all(b["alpha"] is not None for b in er), "alpha is the policy's actual output"


def test_no_bid_ever_exceeds_its_ceiling_over_http(client):
    """The guard invariant, asserted through the serialiser as well as the engine."""
    for state in client.post("/auction", json={}).json()["rounds"]:
        for bid in state["bids"]:
            assert bid["amount"] <= bid["ceiling"] + 1e-9, bid


def test_withdrawals_carry_their_reason(client):
    """A bid that was cut has to be tellable from a bid that was chosen."""
    positions = client.post("/auction", json={}).json()["positions"]
    withdrawn = [p for p in positions.values() if not p["active"]]
    assert withdrawn, "the reference auction has withdrawals"
    assert all(p["exit_reason"] for p in withdrawn)


def test_uplift_off_reverts_to_ceiling_equals_utility(client):
    """``uplift: false`` is D.9's fallback, and it must visibly change the close.

    RL_READINESS section 8.7 publishes both ladders: 95 -> 109 -> 119 with the B.9 interim on,
    85 without it. The flag is the only assumption in the system that can *reallocate* rather
    than reprice, so it has to be reachable and its effect has to be observable.
    """
    on = client.post("/auction", json={}).json()
    off = client.post("/auction", json={"uplift": False}).json()

    assert off["winning_bid"] < on["winning_bid"]
    for cid, ceiling in off["ceilings"].items():
        assert ceiling == pytest.approx(off["utilities"][cid])


def test_rounds_override_is_honoured(client):
    assert client.post("/auction", json={"rounds": 1}).json()["rounds_run"] == 1


def test_a_scenario_is_addressed_by_name(client):
    body = client.post("/auction", json={"scenario": "ward_crash"}).json()
    assert body["world"] != "Appendix C fixture"
    assert body["outcome"] in ("awarded", "no_award")


def test_every_response_says_it_is_neither_persisted_nor_trainable(client):
    """API responses should explicitly mark the run as non-persistent and non-trainable."""
    body = client.post("/auction", json={}).json()
    assert body["audit"]["persisted"] is False
    assert body["reward"]["trainable"] is False
    assert body["governance"]["unsigned_rules"]


# ---------------------------------------------------------------------------------------
# The refusals — the part that is not a convenience
# ---------------------------------------------------------------------------------------


def test_live_mode_is_refused(client):
    """The CLI refuses ``--mode live``; the API must not be the softer door.

    The shipped data source serves three invented patients. A live auction holds a real bed
    and decrements a real budget, so this is the single worst thing the system could be asked
    to do — and over HTTP it would be one curl away.
    """
    answer = client.post("/auction", json={"mode": "live"})
    assert answer.status_code == 403
    assert "live" in answer.json()["error"]


def test_a_session_cannot_be_live_either(client):
    """``run_session`` refuses binding modes itself; nothing in the API can route around it."""
    assert client.post("/session", json={"events": 2}).json()["auctions"] == 2
    assert "mode" not in service.SessionRequest.__dataclass_fields__


@pytest.mark.parametrize(
    "name",
    [
        "../../../etc/passwd",
        "..\\..\\windows\\system32\\config\\sam",
        "/etc/hosts",
        "C:/Windows/win.ini",
        "ward_crash/../../secret",
        "",
    ],
)
def test_a_scenario_name_can_never_be_a_path(client, name):
    """The CLI takes a path because a person with a shell can already read any file.

    Over HTTP that same flag is an arbitrary-file-read primitive for anyone who reaches the
    port, so names are validated against a character class that admits no separator and no
    dot, and the request never gets to build a path at all.
    """
    answer = client.post("/auction", json={"scenario": name})
    assert answer.status_code in (400, 404)
    assert "error" in answer.json()


def test_an_unknown_scenario_is_a_404_that_lists_the_known_ones(client):
    answer = client.post("/auction", json={"scenario": "does_not_exist"})
    assert answer.status_code == 404
    assert "ward_crash" in answer.json()["error"]


def test_an_unresolvable_query_is_a_400_not_a_default_profile(client):
    """Falling back to the ICU profile would score a request against the wrong caps."""
    answer = client.post("/auction", json={"query": "please allocate something"})
    assert answer.status_code == 400
    assert "no registered resource" in answer.json()["error"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"mode": "nonsense"}, 400),
        ({"at": "not-a-timestamp"}, 400),
        ({"rounds": 0}, 422),
        ({"rounds": 999}, 422),
    ],
)
def test_bad_input_is_rejected_with_a_reason(client, body, status):
    assert client.post("/auction", json=body).status_code == status


def test_the_session_event_count_is_bounded(client):
    """A session is synchronous; an unbounded count ties up a worker for minutes."""
    assert client.post("/session", json={"events": 10_000}).status_code == 422
    assert service.MAX_SESSION_EVENTS < 10_000


@pytest.mark.parametrize("every", ["banana", "45", "5x", ""])
def test_a_bad_duration_is_rejected(client, every):
    assert client.post("/session", json={"events": 2, "every": every}).status_code == 400


# ---------------------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------------------


def test_a_session_carries_one_ledger_across_its_auctions(client):
    """The point of a session: a single auction can never show what the budget is for."""
    body = client.post("/session", json={"events": 6, "every": "2h"}).json()

    assert body["auctions"] == 6
    assert len(body["shifts"]) >= 1
    for shift in body["shifts"]:
        for agent in shift["agents"].values():
            assert agent["spent"] >= 0.0
            assert agent["band"] in ("inert", "light", "working", "heavy", "over")


def test_a_session_reports_burn_rate_and_it_is_still_inert(client):
    """F-27, observable through the API rather than asserted from a document.

    At ``common_points: 700`` every department burns a few percent of its allowance, which is
    the finding that blocks RL: if spending is free the optimal policy is "bid your ceiling".
    This test pins the *current* state so that fitting the budget breaks it loudly.
    """
    body = client.post("/session", json={"events": 8, "every": "45m"}).json()
    bands = {
        agent: data["band"]
        for shift in body["shifts"]
        for agent, data in shift["agents"].items()
    }
    assert set(bands.values()) == {"inert"}, (
        f"budgets are no longer inert: {bands}. If common_points was fitted (F-27), this "
        "test has done its job — update it and RL_READINESS section 5.1 together."
    )


# ---------------------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------------------


def test_a_run_can_be_fetched_back_without_re_running_it(client):
    """Re-running would re-read the world, so the second answer need not match the first."""
    posted = client.post("/auction", json={}).json()
    fetched = client.get(f"/auction/{posted['auction_id']}").json()

    assert fetched["winning_bid"] == posted["winning_bid"]
    assert fetched["rounds"] == posted["rounds"]


def test_the_audit_bundle_is_complete(client):
    """Every agent every round, losers included — sections 23-24."""
    posted = client.post("/auction", json={}).json()
    audit = client.get(f"/auction/{posted['auction_id']}/audit").json()

    assert audit["row_count"] == 1 + sum(
        len(r["bids"]) for r in posted["rounds"]
    ) + len(audit["rows"]["budgets"]) + len(audit["rows"]["snapshots"])
    assert audit["persisted"] is False
    assert json.dumps(audit), "the bundle must be JSON-serialisable end to end"


def test_explain_returns_the_derivation_as_text(client):
    posted = client.post("/auction", json={}).json()
    answer = client.get(f"/auction/{posted['auction_id']}/explain")

    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/plain")
    assert "FULL DERIVATION" in answer.text


def test_format_text_returns_the_readable_report(client):
    """The most common client curl has is a person, and JSON is not an answer for one.

    ``?format=text`` returns exactly what ``python -m allocation`` prints — imported from the
    CLI rather than reimplemented, so the terminal and the endpoint cannot drift.
    """
    answer = client.post("/auction?format=text", json={})

    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/plain")
    assert "HOSPILOT allocation" in answer.text
    assert "RESULT" in answer.text
    assert len(answer.text) < 20_000, "the report is a summary; --explain is the long form"


def test_format_text_matches_the_cli_exactly(client, capsys):
    """One renderer. If the API grew its own, this is where the two would separate."""
    assert cli_main([]) == 0
    from_cli = capsys.readouterr().out.strip()
    from_http = client.post("/auction?format=text", json={}).text.strip()

    # The query line and the result line are the two the renderer is judged on; the auction
    # id differs between runs, so a whole-string compare would be testing uuid4.
    assert from_http.splitlines()[1] == from_cli.splitlines()[1]
    assert from_http.splitlines()[-2] == from_cli.splitlines()[-2]


def test_format_steps_is_an_alias_for_text(client):
    """``?format=steps`` and ``?format=text`` are one renderer under two names.

    The deployed instances answer to ``steps``, and the report calls itself "one auction, step
    by step", so a caller who reads the output and guesses the format name guesses ``steps``.
    Compared body-to-body rather than by asserting a marker in each: an alias that quietly
    started rendering something else would pass a marker check.
    """
    from_text = client.post("/auction?format=text", json={})
    from_steps = client.post("/auction?format=steps", json={})

    assert from_steps.status_code == 200
    assert from_steps.headers["content-type"].startswith("text/plain")
    # The auction id differs per run, so the two are compared on the report body ahead of it.
    head = lambda r: r.text.split("2. One read of the world")[0]
    assert head(from_steps) == head(from_text)


def test_an_unknown_format_is_still_refused(client):
    """Adding an alias must not turn the format list into a free-text field."""
    assert client.post("/auction?format=nope", json={}).status_code == 422


def test_format_summary_returns_the_winner_and_why(client):
    """``?format=summary`` is the shortest true answer to "who got the bed".

    One line naming the winner and the bid, one line per bidder saying why it stopped, one
    line for the reserve. No ladder, no components table — that is what ``text`` is for.
    """
    answer = client.post("/auction?format=summary", json={})

    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/plain")
    first = answer.text.splitlines()[0]
    assert first.startswith("winner  er")
    assert "why:" in answer.text
    assert "reserve" in answer.text
    assert len(answer.text) < 1_200, "the summary is a screenful; text is the report"


def test_format_summary_agrees_with_the_json(client):
    """A presentation flag; the summary must name the same winner the JSON does."""
    as_json = client.post("/auction", json={"scenario": "ward_crash"}).json()
    as_summary = client.post(
        "/auction?format=summary", json={"scenario": "ward_crash"}
    ).text

    assert as_summary.startswith(f"winner  {as_json['winner']}")
    assert f"{as_json['winning_bid']:.1f}" in as_summary.splitlines()[0]


def test_format_brief_is_five_lines_a_bare_curl_can_use(client):
    """``?format=brief`` for a caller holding curl and nothing else — no jq, no scripting.

    The shape is the contract: five labelled lines, in this order, one per line. A client
    that greps for ``winner    :`` breaks if a line is added above it or the labels move.
    """
    answer = client.post("/auction?format=brief", json={"query": "one limited ICU bed"})

    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/plain")

    lines = answer.text.strip().splitlines()
    assert [line.split(":")[0].strip() for line in lines] == [
        "query", "resource", "outcome", "winner", "utilities",
    ]
    assert lines[1] == "resource  : icu_bed"
    assert "cleared" in lines[3]


def test_format_brief_agrees_with_the_json_and_ranks_utilities(client):
    """A presentation flag. It must name the JSON's winner, and rank, not just list.

    Descending order is the point: the ranking is what unfitted caps still support, so the
    line has to read as a result rather than a dict in dictionary order.
    """
    body = {"scenario": "ward_crash"}
    as_json = client.post("/auction", json=body).json()
    lines = client.post("/auction?format=brief", json=body).text.strip().splitlines()

    winner = next(line for line in lines if line.startswith("winner"))
    assert as_json["winner"] in winner
    assert f"{as_json['winning_bid']:.1f}" in winner

    utilities = next(line for line in lines if line.startswith("utilities"))
    scores = [
        float(part.strip().rsplit(" ", 1)[1]) for part in utilities.split(":")[1].split("|")
    ]
    assert scores == sorted(scores, reverse=True)
    # ward_crash exists to make Ward the sickest, so it must lead the ranking.
    assert utilities.split(":")[1].strip().startswith("ward")


def test_format_brief_names_the_unit_that_was_auctioned(client):
    """The resource line is the one thing brief says that summary does not.

    With six bed types registered, "who won" is only half an answer — a ward-bed award and
    an ICU-bed award to the same department are different allocations.
    """
    for query, resource in (
        ("one limited ICU bed", "icu_bed"),
        ("an HDU bed has opened up", "hdu_bed"),
        ("a ward bed is free", "ward_bed"),
    ):
        text = client.post("/auction?format=brief", json={"query": query}).text
        assert f"resource  : {resource}" in text, query


def test_format_explain_returns_the_long_derivation(client):
    answer = client.post("/auction?format=explain", json={})

    assert answer.headers["content-type"].startswith("text/plain")
    assert "FULL DERIVATION" in answer.text
    assert len(answer.text) > 10_000


def test_format_text_works_for_a_session(client):
    answer = client.post("/session?format=text", json={"events": 4, "every": "2h"})

    assert answer.headers["content-type"].startswith("text/plain")
    assert "SESSION" in answer.text
    assert "burn" in answer.text


def test_an_unknown_format_is_rejected(client):
    assert client.post("/auction?format=yaml", json={}).status_code == 422


def test_format_does_not_change_the_auction(client):
    """It is a presentation flag; the same request must produce the same allocation."""
    as_json = client.post("/auction", json={"scenario": "ward_crash"}).json()
    as_text = client.post("/auction?format=text", json={"scenario": "ward_crash"}).text

    assert f"{as_json['winner']} wins at {as_json['winning_bid']:.1f}" in as_text


def test_the_derivation_is_available_as_structured_json(client):
    """``--explain`` for a machine: every factor with its weight, value and provenance."""
    posted = client.post("/auction", json={}).json()
    body = client.get(f"/auction/{posted['auction_id']}/derivation").json()

    assert set(body) >= {"rounds", "ceiling", "budget", "bids", "settlement", "formulas"}
    assert len(body["rounds"]) == posted["rounds_run"]
    assert json.dumps(body), "must be JSON-serialisable end to end"


def test_the_derivation_reproduces_the_points_it_reports(client):
    """The arithmetic has to close: numerator / denominator x cap must be the points.

    This is the test that makes the JSON worth returning. A derivation that reports factors
    and points which do not reconcile is worse than no derivation — it looks checkable and
    is not.
    """
    body = client.post("/auction", json={"derivation": True}).json()["derivation"]

    checked = 0
    for state in body["rounds"]:
        for candidate in state["candidates"].values():
            assert candidate["total"] == pytest.approx(
                sum(c["points"] for c in candidate["components"])
            )
            for component in candidate["components"]:
                if component["shape"] != "weighted_mean":
                    continue
                normalised = component["numerator"] / component["denominator"]
                assert normalised == pytest.approx(component["normalised"], abs=1e-9)
                assert component["cap"] * normalised == pytest.approx(component["points"])
                checked += 1

    assert checked > 10, "the fixture should exercise many weighted-mean components"


def test_an_absent_factor_is_null_and_says_why(client):
    """Absent is `null` with a reason, never 0.0 — the whole system rests on this.

    ``time_to_critical`` has no model (B.5), so it carries no value and lowers the weight it
    was going to contribute. A JSON consumer must be able to see that the factor was *not
    measured*, rather than measured as zero.
    """
    body = client.post("/auction", json={"derivation": True}).json()["derivation"]
    factors = [
        factor
        for state in body["rounds"]
        for candidate in state["candidates"].values()
        for component in candidate["components"]
        for factor in component["factors"]
    ]
    absent = [f for f in factors if not f["present"]]

    assert absent, "the fixture has unbuilt models; some factors must be absent"
    assert all(f["value"] is None for f in absent)
    assert all(f["source"] for f in absent), "an absent factor still says where it would come from"
    assert any("B.5" in (f["note"] or "") for f in absent)


def test_coverage_is_the_weight_that_survived(client):
    body = client.post("/auction", json={"derivation": True}).json()["derivation"]

    for state in body["rounds"]:
        for candidate in state["candidates"].values():
            for component in candidate["components"]:
                if "weight_present" not in component:
                    continue
                assert component["coverage"] == pytest.approx(
                    component["weight_present"] / component["weight_total"], abs=1e-6
                )


def test_text_and_json_derivations_are_the_same_numbers(client):
    """One calculation, two renderings — ``component_lines`` renders ``component_derivation``.

    Two implementations would agree until the day they did not, and the disagreement would
    surface as a support question about which page to believe.
    """
    body = client.post("/auction", json={"derivation": True, "explain": True}).json()
    text = body["explain"]

    for component in body["derivation"]["rounds"][0]["candidates"]["ER-Patient-A"]["components"]:
        assert f"{component['points']:+.2f}" in text, component["component"]


def test_an_unknown_auction_id_is_a_404_that_explains_why(client):
    answer = client.get("/auction/00000000-0000-0000-0000-000000000000")
    assert answer.status_code == 404
    assert "F-02" in answer.json()["error"], "say why it is missing, not just that it is"


def test_the_store_is_bounded(client):
    """An unbounded dict in a long-lived process is a memory leak with a nice name."""
    app = create_app(scenario_dir=SCENARIO_DIR, store_size=2)
    small = TestClient(app)
    ids = [small.post("/auction", json={}).json()["auction_id"] for _ in range(4)]

    assert small.get("/auctions").json()["retained"] == 2
    assert small.get(f"/auction/{ids[-1]}").status_code == 200
    assert small.get(f"/auction/{ids[0]}").status_code == 404


# ---------------------------------------------------------------------------------------
# Inline candidates — the difference between a demo and a service
# ---------------------------------------------------------------------------------------

#: A deteriorating septic ER patient and a stable ward one. Deliberately not Appendix C: the
#: point of these tests is that the caller's own patients reach the engine, which a fixture
#: copy could pass without proving.
INLINE_BODY = {
    "hospital": {
        "icu_total_beds": 20,
        "icu_occupied_beds": 19,
        "expected_discharges_4h": 1,
        "boarding_count": 8,
    },
    "candidates": [
        {
            "candidate_id": "MY-ER-1",
            "agent": "er",
            "arrived_at": "-3h",
            "condition_category": "sepsis",
            "severity_band": "severe",
            "needs": ["vasopressors"],
            "vitals": [
                {"at": "-90m", "respiratory_rate": 22, "spo2": 94, "bp_systolic": 108,
                 "pulse": 105, "temperature": 38.4, "gcs": 15},
                {"at": "-20m", "respiratory_rate": 30, "spo2": 89, "bp_systolic": 88,
                 "pulse": 128, "temperature": 39.1, "gcs": 13},
            ],
            "labs": [{"test_name": "lactate", "result_value": 4.6, "at": "-40m"}],
            "orders": [{"medication_name": "noradrenaline", "at": "-25m"}],
        },
        {
            "candidate_id": "MY-WARD-1",
            "agent": "ward",
            "arrived_at": "-9h",
            "condition_category": "pneumonia",
            "severity_band": "moderate",
            "vitals": [
                {"at": "-80m", "respiratory_rate": 20, "spo2": 95, "bp_systolic": 122,
                 "pulse": 92, "gcs": 15},
                {"at": "-15m", "respiratory_rate": 21, "spo2": 95, "bp_systolic": 120,
                 "pulse": 90, "gcs": 15},
            ],
            "labs": [{"test_name": "lactate", "result_value": 1.4, "at": "-60m"}],
        },
    ],
}

KEY = {"X-API-Key": "s3cret-key"}


def _inline(client, body=None, **over):
    return client.post("/auction", json={**(body or INLINE_BODY), **over}, headers=KEY)


def test_the_callers_own_patients_are_the_ones_scored(locked):
    """Without this route the same three fixture patients bid on every call, forever.

    ``query`` selects the *resource profile*, not the patients — four completely different
    sentences return an identical auction. This is the route where the caller's world
    actually reaches the engine.
    """
    body = _inline(locked).json()

    assert set(body["utilities"]) == {"MY-ER-1", "MY-WARD-1"}
    assert body["world"] == "inline candidates"
    assert body["winner"] == "er"


def test_the_clinical_inputs_actually_move_the_score(locked):
    """A septic, deteriorating, vasopressor-dependent patient must outscore a stable one.

    Direction, not magnitude — the caps are unfitted (B.13), so the *size* of the gap is not
    a claim this repository can make. That the gap points the right way is.
    """
    body = _inline(locked).json()
    assert body["utilities"]["MY-ER-1"] > body["utilities"]["MY-WARD-1"] * 2


def test_a_body_can_describe_several_units_and_the_query_picks_one(locked):
    """Step 12 over HTTP. ``hospital.units`` is how a caller supplies more than one unit.

    ``INLINE_BODY`` describes the ICU alone, in the legacy ``icu_*`` spelling, and prices ICU
    beds. Two units in one body means the same patients can be auctioned either bed.
    """
    body = _inline(
        locked,
        query="a ward bed is free",
        hospital={
            "boarding_count": 8,
            "units": [
                {"unit": "icu", "unit_total_beds": 20, "unit_occupied_beds": 19},
                {"unit": "ward", "unit_total_beds": 40, "unit_occupied_beds": 24},
            ],
        },
    ).json()

    ingest = next(step for step in body["trace"] if step["key"] == "ingest")
    rows = dict(tuple(row) for row in ingest["rows"])
    assert rows["WARD occupancy"] == "24/40 = 60%"
    # The hospital-wide field was stated once and reached the unit that was read.
    assert rows["ED boarding"] == "8"


def test_asking_for_a_unit_the_body_did_not_describe_is_a_400(locked):
    """Not a 500, and not ICU's beds. The caller sent a world that cannot answer the query.

    ``INLINE_BODY``'s hospital is the ICU, so a ward-bed query has no ward to price against.
    Substituting the ICU's 19/20 is exactly the bug Step 12 removed, one layer down.
    """
    answer = _inline(locked, query="a ward bed is free")

    assert answer.status_code == 400
    error = answer.json()["error"]
    assert "unit 'ward'" in error
    assert "['icu']" in error, "the error has to say what the body did describe"


def test_absence_survives_the_json_boundary(locked):
    """Omitting a vital must lower coverage, never score it 0.

    This is the reason ``build_scenario`` was extracted rather than a second parser written
    for JSON. Two parsers would be two definitions of absent, and they would drift on exactly
    this case — silently, and in the direction that makes a patient look healthier.
    """
    stripped = json.loads(json.dumps(INLINE_BODY))
    for row in stripped["candidates"][0]["vitals"]:
        del row["spo2"]

    with_spo2 = _inline(locked).json()["utilities"]["MY-ER-1"]
    without = _inline(locked, stripped).json()["utilities"]["MY-ER-1"]

    assert without != with_spo2
    assert without > 0.0, "a missing SpO2 must not read as an SpO2 of zero"


def test_two_candidates_for_one_agent_are_refused_not_dropped(locked):
    """``run_auction`` builds ``{c.agent: c}``, so a second ER patient never bids at all.

    It is not outbid and it is not logged — it is overwritten by a dict comprehension. That is
    correct for the mechanism (RL-Steps runs department against department, one patient each)
    and catastrophic as silent behaviour, so the API refuses it in words.
    """
    duplicate = json.loads(json.dumps(INLINE_BODY))
    duplicate["candidates"][1]["agent"] = "er"

    answer = _inline(locked, duplicate)
    assert answer.status_code == 400
    assert "one candidate per agent" in answer.json()["error"]


def test_inline_candidates_need_a_configured_key(client):
    """The open instance serves fixtures; it must not accept patient data.

    The process cannot tell a real trajectory from an invented one, so it does not try. It
    requires only that somebody decided who may call before any arrives.
    """
    answer = client.post("/auction", json=INLINE_BODY)
    assert answer.status_code == 403
    assert "ALLOCATION_API_KEY" in answer.json()["error"]
    assert client.get("/health").json()["accepts_inline_candidates"] is False


def test_health_advertises_whether_inline_is_open(locked):
    assert locked.get("/health").json()["accepts_inline_candidates"] is True


def test_candidates_and_scenario_are_mutually_exclusive(locked):
    """Both describe the world; serving one silently would be a coin toss the caller loses."""
    answer = _inline(locked, scenario="ward_crash")
    assert answer.status_code == 400
    assert "send one" in answer.json()["error"]


def test_a_hospital_without_candidates_is_refused(locked):
    answer = locked.post(
        "/auction", json={"hospital": {"icu_total_beds": 10, "icu_occupied_beds": 9}},
        headers=KEY,
    )
    assert answer.status_code == 400


def test_a_malformed_candidate_names_the_offending_key(locked):
    broken = json.loads(json.dumps(INLINE_BODY))
    del broken["candidates"][0]["arrived_at"]

    answer = _inline(locked, broken)
    assert answer.status_code == 400
    assert "arrived_at" in answer.json()["error"]
    assert "MY-ER-1" in answer.json()["error"]


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda b: b["candidates"].extend([dict(b["candidates"][0])] * 20), "exceeds the limit"),
        (
            lambda b: b["candidates"][0].__setitem__(
                "vitals", [dict(b["candidates"][0]["vitals"][0])] * 600
            ),
            "the limit is",
        ),
    ],
)
def test_inline_input_is_bounded(locked, mutate, fragment):
    """An auction is synchronous and CPU-bound; the body is the easiest way to make it slow."""
    body = json.loads(json.dumps(INLINE_BODY))
    mutate(body)

    answer = _inline(locked, body)
    assert answer.status_code == 400
    assert fragment in answer.json()["error"]


def test_a_session_can_run_on_inline_candidates(locked):
    """The same patients across a shift — what shows burn against a caller's own case mix."""
    body = locked.post(
        "/session", json={**INLINE_BODY, "events": 4, "every": "2h"}, headers=KEY
    ).json()

    assert body["auctions"] == 4
    assert body["world"] == "inline candidates"


def test_yaml_and_json_scenarios_parse_identically(locked):
    """One parser, proven — the file path and the request body must not diverge.

    ``scenarios/ward_crash.yaml`` sent as a named scenario and the same document sent inline
    have to produce the same auction, or ``build_scenario`` has stopped being shared.
    """
    import yaml

    document = yaml.safe_load((SCENARIO_DIR / "ward_crash.yaml").read_text(encoding="utf-8"))

    from_file = locked.post("/auction", json={"scenario": "ward_crash"}, headers=KEY).json()
    from_body = locked.post(
        "/auction",
        json={"hospital": document["hospital"], "candidates": document["candidates"]},
        headers=KEY,
    ).json()

    assert from_body["utilities"] == from_file["utilities"]
    assert from_body["winner"] == from_file["winner"]
    assert from_body["winning_bid"] == from_file["winning_bid"]


# ---------------------------------------------------------------------------------------
# The API key gate — off by default, total when on
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def locked() -> TestClient:
    return TestClient(create_app(scenario_dir=SCENARIO_DIR, api_key="s3cret-key"))


def test_no_key_configured_means_no_key_required(client):
    """The default stays open, and that is a decision rather than an oversight.

    There is no patient data in this system — three invented Appendix C patients — so a
    credential would be friction protecting nothing. The reasoning inverts the day a real
    DataSource is wired, which is why ``/health`` reports which mode it is in.
    """
    assert client.get("/config").status_code == 200
    assert client.get("/health").json()["auth"] == "none"


def test_a_configured_key_is_required_everywhere_but_health(locked):
    """Health stays open: a liveness probe that needs a secret is one that gets disabled."""
    assert locked.get("/health").status_code == 200
    assert locked.get("/health").json()["auth"] == "api_key"

    for path in ("/config", "/use-cases", "/scenarios", "/auctions"):
        assert locked.get(path).status_code == 401, path
    assert locked.post("/auction", json={}).status_code == 401


def test_a_wrong_key_is_rejected(locked):
    assert locked.get("/config", headers={"X-API-Key": "wrong"}).status_code == 401
    assert locked.get("/config", headers={"X-API-Key": ""}).status_code == 401
    assert locked.get("/config", headers={"X-API-Key": "s3cret-ke"}).status_code == 401


def test_the_right_key_works(locked):
    assert locked.get("/config", headers={"X-API-Key": "s3cret-key"}).status_code == 200
    posted = locked.post("/auction", json={}, headers={"X-API-Key": "s3cret-key"})
    assert posted.status_code == 200
    assert posted.json()["winner"] == "er"


def test_an_empty_env_var_does_not_look_like_a_configured_key():
    """``ALLOCATION_API_KEY=`` in a compose file must not silently disable auth.

    An unset CI secret arrives as an empty string, which is falsy in precisely the way that
    would leave a deployment open while its configuration reads as locked. ``_env`` strips and
    treats blank as absent, so the operator gets the honest "auth: none" rather than a key
    that matches every request that omits the header.
    """
    from allocation.api.__main__ import _env

    os.environ["ALLOCATION_API_KEY"] = "   "
    try:
        assert _env("API_KEY") is None
    finally:
        del os.environ["ALLOCATION_API_KEY"]


# ---------------------------------------------------------------------------------------
# The API and the CLI must not drift
# ---------------------------------------------------------------------------------------


def test_http_and_cli_agree_on_the_same_auction(client, capsys):
    """One implementation, never two.

    ``run_allocation`` is the only path either surface calls, and this is the test that says
    so. If the API ever grows its own arithmetic — a default filled in differently, a rounding
    applied on the way out — the two answers separate and this fails.
    """
    assert cli_main(["--json"]) == 0
    from_cli = json.loads(capsys.readouterr().out)
    from_http = client.post("/auction", json={}).json()

    assert from_http["winner"] == from_cli["winner"]
    assert from_http["winning_bid"] == from_cli["winning_bid"]
    assert from_http["reserve_price"] == from_cli["reserve_price"]
    assert from_http["outcome"] == from_cli["outcome"]
    assert from_http["utilities"] == from_cli["utilities"]
    assert from_http["ceilings"] == from_cli["ceilings"]
    assert from_http["governance"]["caps_version"] == from_cli["caps_version"]
    assert from_http["governance"]["config_version"] == from_cli["config_version"]
