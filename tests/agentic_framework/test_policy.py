"""Autonomy policy engine (workflows/graph/policy.py).

A pure rule engine: given an interrupt context, `evaluate()` returns a Verdict of
auto_approve / require_human / escalate, and `dominant()` collapses the verdicts
of sibling interrupts parked in one superstep. Every test passes an explicit
policy dict, so none of them touch the on-disk rules file.

The bias under test throughout is fail-safe: when the engine is unsure, the flow
must park for a human rather than act on its own.
"""

import pytest

from workflows.graph import policy as p


@pytest.fixture(autouse=True)
def _no_cached_policy():
    """load_policy() memoises for the process lifetime; drop it around every test
    so one test's policy can never leak into the next."""
    p.reset_cache()
    yield
    p.reset_cache()


def _policy(rules, default=p.REQUIRE_HUMAN):
    return {"default": default, "rules": rules}


# ── DEFAULT_POLICY: the built-in, human-safe fallback ────────────────────────

def test_default_policy_risk_tiers():
    d = p.DEFAULT_POLICY
    assert p.evaluate({"risk": "low"}, d).outcome == p.AUTO_APPROVE
    assert p.evaluate({"risk": "medium"}, d).outcome == p.REQUIRE_HUMAN
    assert p.evaluate({"risk": "high"}, d).outcome == p.ESCALATE


def test_default_policy_sensitive_kinds_never_auto_resolve():
    """These are listed ahead of the risk tiers on purpose: even tagged low risk,
    identifying or registering a patient (or a user-requested pause) needs a human."""
    for kind in ("patient_identification", "patient_registration", "user_paused"):
        v = p.evaluate({"kind": kind, "risk": "low"}, p.DEFAULT_POLICY)
        assert v.outcome == p.REQUIRE_HUMAN, f"{kind} auto-resolved"


def test_no_match_falls_back_to_default():
    v = p.evaluate({"risk": "unclassified"}, p.DEFAULT_POLICY)
    assert v.outcome == p.REQUIRE_HUMAN
    assert v.rule is None


# ── matching semantics ───────────────────────────────────────────────────────

def test_first_matching_rule_wins():
    """Rule order is rule priority."""
    pol = _policy([
        {"match": {"risk": "high"}, "outcome": p.AUTO_APPROVE, "decision": "approved"},
        {"match": {"risk": "high"}, "outcome": p.ESCALATE},
    ])
    assert p.evaluate({"risk": "high"}, pol).outcome == p.AUTO_APPROVE


def test_match_is_case_insensitive():
    pol = _policy([{"match": {"kind": "Bed_Assignment"}, "outcome": p.AUTO_APPROVE}])
    assert p.evaluate({"kind": "BED_ASSIGNMENT"}, pol).outcome == p.AUTO_APPROVE
    assert p.evaluate({"kind": "  bed_assignment  "}, pol).outcome == p.AUTO_APPROVE


def test_list_match_is_membership():
    pol = _policy([
        {"match": {"risk": ["low", "medium"]}, "outcome": p.AUTO_APPROVE},
    ])
    assert p.evaluate({"risk": "medium"}, pol).outcome == p.AUTO_APPROVE
    assert p.evaluate({"risk": "high"}, pol).outcome == p.REQUIRE_HUMAN


def test_all_keys_must_match():
    """A rule is an AND across its match keys — a partial match must not fire."""
    pol = _policy([
        {"match": {"kind": "staff", "risk": "low"}, "outcome": p.AUTO_APPROVE},
    ])
    assert p.evaluate({"kind": "staff", "risk": "low"}, pol).outcome == p.AUTO_APPROVE
    assert p.evaluate({"kind": "staff", "risk": "high"}, pol).outcome == p.REQUIRE_HUMAN
    assert p.evaluate({"kind": "staff"}, pol).outcome == p.REQUIRE_HUMAN


def test_missing_context_key_does_not_match():
    """An absent key must never be treated as a wildcard."""
    pol = _policy([{"match": {"agent_id": "bed_agent"}, "outcome": p.AUTO_APPROVE}])
    assert p.evaluate({}, pol).outcome == p.REQUIRE_HUMAN


def test_non_string_payload_extras_match_by_value():
    """Interrupt payload extras (bed_id, count, ...) arrive as ints/bools."""
    pol = _policy([{"match": {"count": 3}, "outcome": p.AUTO_APPROVE}])
    assert p.evaluate({"count": 3}, pol).outcome == p.AUTO_APPROVE
    assert p.evaluate({"count": 4}, pol).outcome == p.REQUIRE_HUMAN


def test_empty_match_is_a_catch_all():
    """`all()` over no keys is True, so `{}` matches everything — the documented
    way to write a terminal rule."""
    pol = _policy([{"match": {}, "outcome": p.ESCALATE}])
    assert p.evaluate({"anything": "at all"}, pol).outcome == p.ESCALATE


def test_verdict_carries_the_matched_rule_and_a_reason():
    """The reason string is what reaches the websocket and the audit row."""
    rule = {"match": {"risk": "low"}, "outcome": p.AUTO_APPROVE, "decision": "ok"}
    v = p.evaluate({"risk": "low"}, _policy([rule]))
    assert v.rule["match"] == {"risk": "low"}
    assert v.decision == "ok"
    assert v.reason


# ── loading / normalisation robustness ───────────────────────────────────────

def test_normalise_drops_malformed_rules_and_bad_default():
    """A partly-bad file must still yield a usable policy rather than raising."""
    got = p._normalise({
        "default": "nonsense",
        "rules": [
            "not-an-object",
            {"match": {"risk": "low"}, "outcome": "teleport"},   # bad outcome
            {"match": "not-an-object", "outcome": p.AUTO_APPROVE},
            {"match": {"risk": "low"}, "outcome": p.AUTO_APPROVE},  # the only good one
        ],
    })
    assert got["default"] == p.REQUIRE_HUMAN
    assert len(got["rules"]) == 1
    assert got["rules"][0]["match"] == {"risk": "low"}


def test_normalise_applies_rule_defaults():
    got = p._normalise({"rules": [{"match": {}, "outcome": p.AUTO_APPROVE}]})
    assert got["rules"][0]["decision"] == "approved"
    assert got["rules"][0]["notify"] is True


def test_normalise_rejects_a_non_object_root():
    with pytest.raises(ValueError):
        p._normalise(["not", "a", "dict"])


def test_normalise_tolerates_missing_and_null_rules():
    for raw in ({}, {"rules": None}, {"rules": []}):
        assert p._normalise(raw)["rules"] == []


# ── dominant(): collapsing sibling verdicts ──────────────────────────────────

def test_dominant_unanimous_auto_approve():
    vs = [p.Verdict(outcome=p.AUTO_APPROVE, decision="approved") for _ in range(3)]
    assert p.dominant(vs).outcome == p.AUTO_APPROVE


def test_dominant_highest_severity_wins():
    vs = [
        p.Verdict(outcome=p.AUTO_APPROVE),
        p.Verdict(outcome=p.REQUIRE_HUMAN),
        p.Verdict(outcome=p.ESCALATE),
    ]
    assert p.dominant(vs).outcome == p.ESCALATE


def test_dominant_mixed_auto_decisions_parks_for_human():
    """All siblings auto-approve, but with different resume values. One
    Command(resume=...) can't satisfy both, so park rather than mis-deliver."""
    vs = [
        p.Verdict(outcome=p.AUTO_APPROVE, decision="approved"),
        p.Verdict(outcome=p.AUTO_APPROVE, decision="rejected"),
    ]
    assert p.dominant(vs).outcome == p.REQUIRE_HUMAN


def test_dominant_single_verdict_passes_through_untouched():
    v = p.Verdict(outcome=p.AUTO_APPROVE, decision="approved", reason="only one")
    assert p.dominant([v]) is v


def test_dominant_empty_is_require_human():
    assert p.dominant([]).outcome == p.REQUIRE_HUMAN


# ── fail-safe ────────────────────────────────────────────────────────────────

def test_evaluate_never_raises_on_bad_policy():
    """Policy evaluation must never break a run: anything unusable parks for a
    human instead of propagating."""
    for bad in ({"rules": [{"no_match_key": 1}]}, {"rules": "not-a-list"},
                {"rules": [None]}, {"rules": [{"match": {}}]}):
        assert p.evaluate({"risk": "low"}, bad).outcome == p.REQUIRE_HUMAN


def test_evaluate_with_no_rules_uses_the_stated_default():
    """An empty rule list isn't an error — it's a policy that defers everything."""
    assert p.evaluate({"risk": "low"}, {"rules": []}).outcome == p.REQUIRE_HUMAN
    assert p.evaluate({"risk": "low"}, {"default": p.ESCALATE, "rules": []}).outcome == p.ESCALATE


def test_evaluate_survives_an_exploding_context():
    class Boom:
        def __str__(self):  # noqa: D105
            raise RuntimeError("cannot stringify")

    pol = _policy([{"match": {"risk": "low"}, "outcome": p.AUTO_APPROVE}])
    assert p.evaluate({"risk": Boom()}, pol).outcome == p.REQUIRE_HUMAN
