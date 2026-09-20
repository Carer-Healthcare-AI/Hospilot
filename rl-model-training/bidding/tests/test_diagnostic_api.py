"""The ``/diagnostic/*`` HTTP surface.

These routes are what the Hospilot gateway posts to, so the tests are written against the
response *shape* a caller depends on rather than against the numbers a scenario happens to
produce — a scenario whose utilisation changes should not break an integration.

Two properties are asserted hard, because both are safety claims rather than conveniences:
inline requests carry patient data and are refused without a configured key, and a query the
resolver cannot ground refuses with its evidence instead of inventing a clinical value.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from allocation.api.app import create_app

T0 = "2026-08-25T08:00:00+00:00"
T_END = "2026-08-25T16:00:00+00:00"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def keyed() -> TestClient:
    return TestClient(create_app(api_key="test-key"))


HEADERS = {"X-API-Key": "test-key"}


def _request(request_id: str, agent: str, latest_useful_at: str) -> dict:
    return {
        "request_id": request_id,
        "patient_token": f"tok-{request_id}",
        "agent": agent,
        "clinical_question": "suspected intracranial bleed",
        "requested_procedure": "CT head",
        "eligible_modalities": ["ct"],
        "requested_at": T0,
        "latest_useful_at": latest_useful_at,
        "estimated_duration_minutes": 15,
        "diagnostic_yield": 0.7,
        "management_impact_probability": 0.6,
        "management_impact_importance": 0.8,
        "required_capabilities": [],
    }


def _machine() -> dict:
    return {
        "machine_id": "CT-01",
        "modality": "ct",
        "status": "available",
        "window_starts_at": T0,
        "window_ends_at": T_END,
        "capabilities": [],
        "setup_minutes": 3,
        "cleanup_minutes": 3,
    }


def _inline_body() -> dict:
    return {
        "machines": [_machine()],
        "requests": [
            _request("er-1", "er", "2026-08-25T09:00:00+00:00"),
            _request("icu-1", "icu", "2026-08-25T11:00:00+00:00"),
            _request("ot-1", "ot", "2026-08-25T13:00:00+00:00"),
        ],
        "regime": "normal",
    }


# --- discovery ---------------------------------------------------------------------------


def test_modalities_lists_the_four_families(client):
    body = client.get("/diagnostic/modalities").json()
    assert {m["modality"] for m in body["modalities"]} == {"ct", "mri", "x_ray", "ultrasound"}
    assert body["regimes"] == ["normal", "constrained", "exhausted"]


def test_every_modality_names_its_own_caps_and_budget_tables(client):
    """The bed family's mistake in reverse: sharing one caps table across resources."""
    mods = client.get("/diagnostic/modalities").json()["modalities"]
    assert len({m["caps_config"] for m in mods}) == len(mods)
    assert len({m["budget_config"] for m in mods}) == len(mods)


def test_scenarios_are_discoverable_and_say_what_they_demonstrate(client):
    scenarios = client.get("/diagnostic/scenarios").json()["scenarios"]
    assert scenarios
    assert all(s["name"] and s["modality"] and s["expects"] for s in scenarios)


# --- running a fixture -------------------------------------------------------------------


def test_a_named_scenario_runs_and_returns_a_schedule(client):
    body = client.post("/diagnostic/auction", json={"scenario": "three_way_contention"}).json()
    assert body["family"] == "diagnostic_machine"
    assert body["modality"] == "ct"
    assert body["binding"] is False
    assert body["metrics"]["auctions"] >= 1
    assert body["schedule"], "a contested scenario should place at least one procedure"
    row = body["schedule"][0]
    assert {"request_id", "agent", "machine_id", "starts_at", "ends_at"} <= set(row)


def test_an_unknown_scenario_names_the_ones_that_exist(client):
    r = client.post("/diagnostic/auction", json={"scenario": "no_such_scenario"})
    assert r.status_code == 422
    assert "available" in r.json()


def test_an_unknown_regime_is_refused(client):
    r = client.post(
        "/diagnostic/auction", json={"scenario": "three_way_contention", "regime": "wishful"}
    )
    assert r.status_code == 422


def test_derivation_returns_the_full_trace(client):
    body = client.post(
        "/diagnostic/auction",
        json={"scenario": "three_way_contention", "derivation": True},
    ).json()
    assert "derivation" in body and body["derivation"]


# --- naming the world --------------------------------------------------------------------


def test_the_world_must_be_named_exactly_once(client):
    assert client.post("/diagnostic/auction", json={}).status_code == 422
    both = client.post(
        "/diagnostic/auction", json={"scenario": "three_way_contention", "query": "ct please"}
    )
    assert both.status_code == 422
    assert set(both.json()["received"]) == {"scenario", "query"}


def test_an_ungrounded_query_refuses_with_its_evidence(client):
    """It must not invent the half of the world the sentence did not carry."""
    r = client.post("/diagnostic/auction", json={"query": "who should get something"})
    assert r.status_code == 422
    body = r.json()
    assert "missing" in body and "evidence" in body


# --- inline state ------------------------------------------------------------------------


def test_inline_requests_are_refused_without_a_configured_key(client):
    r = client.post("/diagnostic/auction", json=_inline_body())
    assert r.status_code == 403
    assert "ALLOCATION_API_KEY" in r.json()["error"]


def test_inline_auction_returns_the_whole_ladder(keyed):
    body = keyed.post("/diagnostic/auction", json=_inline_body(), headers=HEADERS).json()
    assert body["winner"] and body["winning_request_id"]
    assert body["awarded_interval"]["machine_id"] == "CT-01"
    # every bidder appears in the opening round, losers included
    agents = {bid["agent"] for bid in body["rounds"][0]["bids"]}
    assert agents == {"er", "icu", "ot"}


def test_the_award_honours_setup_and_cleanup(keyed):
    """A 15-minute procedure on a machine with 3+3 occupies 21 minutes, not 15."""
    from datetime import datetime

    body = keyed.post("/diagnostic/auction", json=_inline_body(), headers=HEADERS).json()
    interval = body["awarded_interval"]
    minutes = (
        datetime.fromisoformat(interval["ends_at"])
        - datetime.fromisoformat(interval["starts_at"])
    ).total_seconds() / 60
    assert minutes == 21


def test_the_response_stamps_the_versions_it_ran_under(keyed):
    body = keyed.post("/diagnostic/auction", json=_inline_body(), headers=HEADERS).json()
    assert body["caps_version"] and body["config_version"]


def test_one_auction_covers_one_modality(keyed):
    body = _inline_body()
    second = _machine() | {"machine_id": "MRI-01", "modality": "mri"}
    body["machines"].append(second)
    r = keyed.post("/diagnostic/auction", json=body, headers=HEADERS)
    assert r.status_code == 422
    assert "one modality" in r.json()["error"]


def test_a_missing_clinical_value_is_refused_not_defaulted(keyed):
    """Defaulting here would rank a real patient against an invented number."""
    body = _inline_body()
    del body["requests"][0]["diagnostic_yield"]
    r = keyed.post("/diagnostic/auction", json=body, headers=HEADERS)
    assert r.status_code == 422
    assert "diagnostic_yield" in r.json()["error"]


@pytest.mark.parametrize("field", ["clinical_question", "requested_procedure", "patient_token"])
def test_a_contract_violation_is_a_refusal_not_a_crash(keyed, field):
    """The request contracts enforce invariants the parser does not restate.

    Those are caller errors and must come back as 422 naming the field. Before this was
    handled, a missing `clinical_question` reached the dataclass and escaped as a 500 — the
    body was wrong, but the response said the server was.
    """
    body = _inline_body()
    del body["requests"][0][field]
    r = keyed.post("/diagnostic/auction", json=body, headers=HEADERS)
    assert r.status_code == 422
    assert field in r.json()["error"]


def test_an_unknown_agent_names_the_valid_ones(keyed):
    body = _inline_body()
    body["requests"][0]["agent"] = "radiology"
    r = keyed.post("/diagnostic/auction", json=body, headers=HEADERS)
    assert r.status_code == 422
    assert "available" in r.json()


def test_an_auction_needs_a_machine_and_a_request(keyed):
    empty_machines = _inline_body() | {"machines": []}
    # with no machines the body names no world at all, which is the earlier refusal
    assert keyed.post(
        "/diagnostic/auction", json=empty_machines, headers=HEADERS
    ).status_code == 422

    no_requests = _inline_body() | {"requests": []}
    r = keyed.post("/diagnostic/auction", json=no_requests, headers=HEADERS)
    assert r.status_code == 422
    assert "request" in r.json()["error"]


# --- family separation -------------------------------------------------------------------


def test_the_bed_routes_do_not_advertise_diagnostic_resources(client):
    """`/use-cases` is the bed registry. A scanner is not a bed and must not appear there."""
    resources = {p["resource_type"] for p in client.get("/use-cases").json()["profiles"]}
    assert resources == {"ed_bed", "hdu_bed", "icu_bed", "pacu_bed", "resus_bed", "ward_bed"}
