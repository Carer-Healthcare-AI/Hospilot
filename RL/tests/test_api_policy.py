"""The learned policy over HTTP.

RL_FIXES fix 6 made trained weights loadable at serve time and patched ``cli.py`` only, so
``POST /auction`` went on serving the heuristic whatever the process had been started with.
These tests are about the property that gap violated: both surfaces reach one policy
implementation, and the safety ordering around it does not weaken by arriving over a port.

Kept out of ``test_api.py`` because every test here needs a differently-configured process —
loaded/not, shadowing/acting — and that is a different fixture axis from the rest of the
suite, which shares one client.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="the HTTP API is an optional extra")

from fastapi.testclient import TestClient  # noqa: E402

from allocation.api import service  # noqa: E402
from allocation.api.app import create_app  # noqa: E402
from allocation.cli import main as cli_main  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "scenarios"
POLICY = ROOT / "artifacts" / "er_policy.json"

requires_weights = pytest.mark.skipif(
    not POLICY.is_file(), reason="no trained weights on disk; train_er.py has not been run"
)


@pytest.fixture(scope="module")
def plain() -> TestClient:
    """Started without ``--policy`` — the default deployment."""
    return TestClient(create_app(scenario_dir=SCENARIO_DIR))


@pytest.fixture(scope="module")
def shadowing() -> TestClient:
    return TestClient(create_app(scenario_dir=SCENARIO_DIR, policy_path=POLICY))


@pytest.fixture(scope="module")
def acting() -> TestClient:
    return TestClient(
        create_app(scenario_dir=SCENARIO_DIR, policy_path=POLICY, policy_live=True)
    )


# ---------------------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------------------


def test_default_is_still_the_heuristic(plain):
    """Adding a policy knob must not promote one."""
    body = plain.post("/auction", json={}).json()
    assert body["policy"]["names"] == ["heuristic"]
    assert body["policy"]["shadow"] is None


def test_rl_without_weights_is_a_409_not_a_silent_heuristic(plain):
    """The failure mode that matters: asking for RL and quietly getting something else.

    Returning the heuristic's ladder to a request that asked for the learned one is the bug
    this endpoint exists to make impossible.
    """
    response = plain.post("/auction", json={"policy": "rl"})
    assert response.status_code == 409
    assert "--policy" in response.json()["error"]


def test_health_says_what_this_process_will_serve(plain):
    assert plain.get("/health").json()["policy"] == {
        "loaded": False,
        "acting": False,
        "default_for_requests": "heuristic",
        "note": "started without --policy",
    }


def test_an_unknown_policy_name_is_rejected(plain):
    """Rejected by the ``Literal`` before ``service`` is reached, hence 422 not 400."""
    assert plain.post("/auction", json={"policy": "dqn"}).status_code == 422


# ---------------------------------------------------------------------------------------
# Shadow — loaded, deciding nothing
# ---------------------------------------------------------------------------------------


@requires_weights
def test_health_reports_the_artefact_and_its_posture(shadowing):
    """Versions on ``/health`` because a caller cannot infer them from a bid ladder."""
    policy = shadowing.get("/health").json()["policy"]
    assert policy["loaded"] is True
    assert policy["acting"] is False
    assert policy["mode"] == "shadow"
    assert policy["policy_version"] == "rl-linear-v1"
    assert policy["encoder_version"] and policy["fabrication_version"]
    assert policy["safety_clinically_approved"] is False
    # C-5 travels with the artefact rather than living only in RL_FIXES.md.
    assert "F-01" in policy["note"]


@requires_weights
def test_shadow_records_without_deciding(shadowing, plain):
    """``policy: rl`` on a shadowing process must not change who wins or what it pays.

    Asserted against the heuristic's own numbers rather than a constant, so it keeps holding
    when the caps are refitted.
    """
    heuristic = plain.post("/auction", json={}).json()
    shadowed = shadowing.post("/auction", json={"policy": "rl"}).json()

    assert shadowed["winner"] == heuristic["winner"]
    assert shadowed["winning_bid"] == heuristic["winning_bid"]
    assert shadowed["policy"]["names"] == ["shadow(heuristic|rl:rl-linear-v1)"]


@requires_weights
def test_the_shadow_log_reaches_the_caller(shadowing):
    """Over HTTP it can only do that in the body.

    ``cli._report_shadow`` writes to stderr to keep ``--json`` parseable; there is no second
    stream here, and a shadowed run whose divergence the caller cannot see recorded nothing
    they can act on.
    """
    report = shadowing.post("/auction", json={"policy": "rl"}).json()["policy"]["shadow"]
    assert report["decisions_observed"] > 0
    assert 0.0 <= report["divergence_rate"] <= 1.0
    assert report["breaker_threshold"] == 0.35
    assert report["gate_refusals"] == []


@requires_weights
def test_a_session_runs_the_learned_policy_at_every_event(shadowing):
    """A session is the shortest request that can trip the divergence breaker.

    ``DivergenceMonitor.tripped`` needs thirty decisions before it reports at all, which one
    auction cannot supply — so a shadow report available only on ``/auction`` could never
    show a caller the thing it exists to show them.
    """
    body = shadowing.post("/session", json={"policy": "rl", "events": 12}).json()
    assert body["policy"]["names"] == ["shadow(heuristic|rl:rl-linear-v1)"]
    assert body["policy"]["shadow"]["decisions_observed"] >= 30
    assert body["policy"]["shadow"]["disagreements"]


# ---------------------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------------------


@requires_weights
def test_live_policy_actually_decides(acting):
    """And is distinguishable from the heuristic inside the same process."""
    learned = acting.post("/auction", json={"policy": "rl"}).json()
    heuristic = acting.post("/auction", json={"policy": "heuristic"}).json()

    assert learned["policy"]["names"] == ["gated(rl:rl-linear-v1)"]
    assert heuristic["policy"]["names"] == ["heuristic"]
    assert learned["winning_bid"] != heuristic["winning_bid"]


@requires_weights
def test_http_and_cli_agree_under_the_learned_policy(acting, capsys):
    """The fix-6 property, extended to the surface fix 6 missed.

    ``test_api.py`` pins HTTP against CLI on the heuristic path. Without this one the API
    could select, gate or construct the learned policy differently and every existing test
    would still pass.
    """
    assert cli_main(["--json", "--policy", str(POLICY), "--live-policy"]) == 0
    from_cli = json.loads(capsys.readouterr().out)
    from_http = acting.post("/auction", json={"policy": "rl"}).json()

    assert from_http["winner"] == from_cli["winner"]
    assert from_http["winning_bid"] == from_cli["winning_bid"]
    assert from_http["reserve_price"] == from_cli["reserve_price"]
    assert from_http["utilities"] == from_cli["utilities"]


@requires_weights
def test_a_retained_run_still_names_the_policy_that_decided_it(acting):
    """Read off the audit rows, so it outlives the request that produced it."""
    auction_id = acting.post("/auction", json={"policy": "rl"}).json()["auction_id"]
    retained = acting.get(f"/auction/{auction_id}").json()
    assert retained["policy"]["names"] == ["gated(rl:rl-linear-v1)"]


# ---------------------------------------------------------------------------------------
# The text report — where a reader, not a parser, meets the result
# ---------------------------------------------------------------------------------------


def test_the_plain_report_is_unchanged_without_a_policy(plain):
    """No banner where there is nothing to declare."""
    report = plain.post("/auction?format=text", json={}).text
    assert report.startswith("=" * 78)
    assert "PROVISIONAL SAFETY RULES" not in report


@requires_weights
def test_omitting_policy_uses_the_weights_the_process_loaded(acting):
    """A server started to serve weights serves them without every caller having to ask.

    This is the whole point of the resolved default: an operator who passed ``--policy`` and
    then read a heuristic ladder had no way to tell a misconfigured server from a working one.
    """
    d = acting.post("/auction", json={}).json()
    assert d["policy"]["names"] == ["gated(rl:rl-linear-v1)"]


@requires_weights
def test_a_request_can_still_ask_for_the_heuristic(acting):
    """The default moved; the choice did not. Naming ``heuristic`` must still get it."""
    d = acting.post("/auction", json={"policy": "heuristic"}).json()
    assert d["policy"]["names"] == ["heuristic"]


def test_a_process_without_weights_still_answers_with_the_heuristic(plain):
    """Resolving the default must not turn a weightless server into an all-409 server."""
    d = plain.post("/auction", json={}).json()
    assert d["policy"]["names"] == ["heuristic"]


@requires_weights
def test_health_reports_the_resolved_default(acting):
    """The default is now a property of startup, so it cannot be read off the schema."""
    assert acting.get("/health").json()["policy"]["default_for_requests"] == "rl"


@requires_weights
def test_the_acting_report_declares_the_policy_before_the_numbers(acting):
    """The CLI's stderr header, in the body, above the report.

    A text rendering that omitted it would be the only view of an auction that does not say a
    learned policy decided it — and it is the view a person actually reads.

    The provisional-safety wall is *not* asserted here: it is gated behind
    ``service.SHOW_SAFETY_BANNER``, which ships off. What may never go missing is the line
    naming the bidder, so that is what this pins. The safety posture is covered structurally
    by ``test_health_reports_the_safety_posture`` and, when the wall is switched back on, by
    ``test_the_safety_banner_can_be_restored``.
    """
    report = acting.post("/auction?format=text", json={"policy": "rl"}).text
    head, _, rest = report.partition("HOSPILOT allocation")

    assert "policy   gated(rl:rl-linear-v1)" in head
    assert "ACTING — the learned policy decides this allocation" in head
    # ...and the report itself is still the report.
    assert "1. Use case resolved" in rest
    assert "12. Reward — scheduled, not scored" in rest


@requires_weights
def test_the_acting_report_omits_the_safety_wall_by_default(acting):
    """``SHOW_SAFETY_BANNER`` ships off, so the wall does not repeat above every response."""
    report = acting.post("/auction?format=text", json={"policy": "rl"}).text
    assert "PROVISIONAL SAFETY RULES" not in report
    assert report.startswith("policy   gated(rl:rl-linear-v1)")


@requires_weights
def test_the_safety_banner_can_be_restored(acting, monkeypatch):
    """Flipping the constant brings the wall back, rules and thresholds intact.

    Pinned because the constant is the documented way back: a reader who flips it and got
    nothing would have no way to tell a dead switch from a satisfied condition.
    """
    monkeypatch.setattr(service, "SHOW_SAFETY_BANNER", True)
    report = acting.post("/auction?format=text", json={"policy": "rl"}).text
    head, _, _ = report.partition("HOSPILOT allocation")

    assert "PROVISIONAL SAFETY RULES — 3 in force, NONE clinically approved." in head
    assert "never_abandon_at_or_above_news2  (threshold 7.0)" in head
    assert "policy   gated(rl:rl-linear-v1)" in head


@requires_weights
def test_the_shadow_report_says_shadowing_and_appends_the_divergence(shadowing):
    """No safety banner: nothing is being decided, so there is nothing to warn about."""
    report = shadowing.post("/auction?format=text", json={"policy": "rl"}).text

    assert report.startswith("policy   shadow(heuristic|rl:rl-linear-v1)")
    assert "PROVISIONAL SAFETY RULES" not in report
    assert "SHADOW — what the learned policy would have done" in report
    assert "decisions observed" in report
    assert "breaker" in report


@requires_weights
def test_the_api_text_report_matches_the_cli(acting, capsys):
    """Same header, same body, same order — one rendering, reached two ways."""
    assert cli_main(["--policy", str(POLICY), "--live-policy"]) == 0
    captured = capsys.readouterr()
    from_cli = captured.err + captured.out
    from_http = acting.post("/auction?format=text", json={"policy": "rl"}).text

    def shape(text: str) -> list[str]:
        """Section headers only — the numbers move with the fixture clock, the shape does not."""
        return [
            line.strip()
            for line in text.splitlines()
            if line.strip() and (line[:1].isdigit() or line.startswith("policy   "))
        ]

    assert shape(from_http) == shape(from_cli)


# ---------------------------------------------------------------------------------------
# Startup refusals — an operator error must not surface as a client error
# ---------------------------------------------------------------------------------------


def test_live_policy_without_weights_refuses_to_start():
    with pytest.raises(service.ApiError) as caught:
        create_app(scenario_dir=SCENARIO_DIR, policy_live=True)
    assert "--live-policy requires --policy" in caught.value.message


def test_missing_weights_refuse_to_start(tmp_path):
    with pytest.raises(service.ApiError) as caught:
        create_app(scenario_dir=SCENARIO_DIR, policy_path=tmp_path / "absent.json")
    assert caught.value.status == 500


@requires_weights
def test_weights_from_another_encoder_refuse_to_start(tmp_path):
    """F-24. A policy served a different state vector is undefined, not degraded.

    ``QWeights.load`` already refuses this; what is asserted here is that the API calls it at
    startup rather than at first use, so a stale artefact cannot be discovered by a caller.
    """
    stale = json.loads(POLICY.read_text(encoding="utf-8"))
    stale["encoder_version"] = "0" * 12
    path = tmp_path / "stale.json"
    path.write_text(json.dumps(stale), encoding="utf-8")

    with pytest.raises(service.ApiError) as caught:
        create_app(scenario_dir=SCENARIO_DIR, policy_path=path)
    assert "encoder" in caught.value.message
