import json

import pytest

from allocation.config import load_config
from allocation.use_cases.diagnostic_machine.evaluate import run_policy
from allocation.use_cases.diagnostic_machine.explain import as_data, as_json, explain
from allocation.use_cases.diagnostic_machine.query import resolve
from allocation.use_cases.diagnostic_machine.scenarios import three_way_contention


@pytest.fixture(scope="module")
def traced():
    config = load_config()
    scenario = three_way_contention()
    result = run_policy(config, scenario, None, regime="normal")
    return config, scenario, result


def test_trace_contains_every_auction_round_and_bidder(traced):
    config, scenario, result = traced
    data = as_data(result, scenario, config)
    assert len(data["auctions"]) == len(result.auctions)
    assert sum(len(r["bids"]) for a in data["auctions"] for r in a["rounds"]) == sum(
        len(r.bids) for a in result.auctions for r in a.rounds
    )
    assert {b["agent"] for b in data["auctions"][0]["rounds"][0]["bids"]} == {
        "er", "icu", "ot"
    }


def test_trace_arithmetic_reproduces_runtime_values(traced):
    config, scenario, result = traced
    data = as_data(result, scenario, config)
    for auction in data["auctions"]:
        for round_ in auction["rounds"]:
            for bid in round_["bids"]:
                assert sum(c["points"] for c in bid["components"]) == pytest.approx(bid["utility"])
                if bid["alpha"] is not None:
                    assert bid["alpha"] * bid["headroom"] == pytest.approx(bid["increment"])
                    assert bid["previous_bid"] + bid["increment"] == pytest.approx(
                        bid["proposed_bid"]
                    )
        for row in auction["settlement"]:
            assert (
                row["bid"] * row["contention"] * row["outcome_factor"]
                * row["commitment_rate"]
            ) == pytest.approx(row["cost"])


def test_text_and_json_are_deterministic_and_complete(traced):
    config, scenario, result = traced
    first_text = explain(result, scenario, config)
    first_json = as_json(result, scenario, config)
    assert first_text == explain(result, scenario, config)
    assert first_json == as_json(result, scenario, config)
    assert "ROUND 1" in first_text
    assert "SETTLEMENT" in first_text
    assert "REWARDS" in first_text
    assert json.loads(first_json)["scenario"] == "three_way_contention"


def test_query_routes_clear_intent_without_inventing_values():
    resolved = resolve("ER ICU and OT compete for one CT scanner")
    assert resolved.runnable
    assert resolved.scenario == "three_way_contention"


def test_query_refuses_ambiguous_text():
    unresolved = resolve("please allocate some diagnostic capacity")
    assert not unresolved.runnable
    assert unresolved.missing
