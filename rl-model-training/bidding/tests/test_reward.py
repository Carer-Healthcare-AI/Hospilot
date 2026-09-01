"""The reward layer against RL-Steps sections 21, 23 and 24.

Most of these tests are about **not scoring what was not observed**. The reward terms are the
objective function: a term wrongly awarded teaches the policy to want the wrong thing, and
unlike a mis-set threshold it does so invisibly and permanently.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from allocation.contracts import AgentKind
from allocation.reward import (
    Step,
    build_episode,
    due,
    load_terms,
    maximum_reward,
    minimum_reward,
    pending_for,
    score,
    trainable,
    unobservable,
)

NOW = datetime(2026, 8, 7, 13, 6, tzinfo=timezone.utc)

#: Section 23 — every positive condition observed true, and the counterfactual ones false.
SECTION_23 = {
    "transferred_to_icu": True,
    "patient_stabilised": True,
    "boarding_reduced": True,
    "cubicle_released": True,
    "safely_held": True,
    "second_bed_opened": True,
    "surgery_not_cancelled": True,
    "no_staffing_violation": True,
    "patient_deterioration": False,
    "additional_boarding": False,
    "emergency_escalation": False,
    "ot_throughput": False,
    "revenue": False,
}

#: Section 24 — the episode where ER lost.
SECTION_24 = {
    "patient_deterioration": True,
    "additional_boarding": True,
    "emergency_escalation": True,
    "ot_throughput": True,
    "revenue": True,
    "transferred_to_icu": False,
    "patient_stabilised": False,
    "boarding_reduced": False,
    "cubicle_released": False,
    "safely_held": False,
    "second_bed_opened": False,
    "surgery_not_cancelled": False,
    "no_staffing_violation": False,
}


# ---------------------------------------------------------------------------------------
# The terms
# ---------------------------------------------------------------------------------------


def test_section_23_terms_are_loaded_with_their_points(config):
    terms = load_terms(config)
    assert terms["transferred_to_icu"].points == 50
    assert terms["patient_stabilised"].points == 40
    assert terms["no_mortality"].points == 30
    assert terms["patient_deterioration"].points == -60


def test_section_23_does_not_add_up(config):
    """BUILD_SPEC F-22. Nine positive terms sum to 200; section 23 states R = 190.

    The terms as listed are authoritative — a stated total cannot override the line items it
    is a total of. Flagged rather than silently reconciled, because if the intent was 190 then
    one of the nine terms is wrong and only a clinician can say which.
    """
    assert maximum_reward(config) == 200
    assert minimum_reward(config) == -100


def test_only_no_mortality_has_no_source(config):
    """F-01 — eight of the nine section 23 terms map to live tables."""
    terms = load_terms(config)
    without_source = {name for name, term in terms.items() if not term.observable}
    assert without_source == {"no_mortality"}
    assert unobservable(config) == ("no_mortality",)


# ---------------------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------------------


def test_section_23_scores_170_without_mortality(config):
    """The winning episode, minus the one term that cannot be observed.

    200 available, 30 of it unobservable, so a fully observed win scores 170 and is still
    marked incomplete.
    """
    row = score(config, "a1", SECTION_23, observed_at=NOW)
    assert row.reward_total == 170
    assert row.complete is False
    assert row.missing_terms == ("no_mortality",)


def test_section_24_scores_minus_65(config):
    """The losing episode. RL-Steps' own arithmetic here is correct: -60-20-20+25+10."""
    row = score(config, "a1", SECTION_24, observed_at=NOW)
    assert row.reward_total == -65
    assert "In similar states, ER should bid more aggressively earlier."


def test_mortality_known_completes_the_episode(config):
    """Once a disposition field exists, the same observations become usable."""
    row = score(
        config, "a1", SECTION_23, observed_at=NOW,
        mortality_observed=True, mortality_source="ipd_admissions.disposition",
    )
    assert row.reward_total == 200
    assert row.complete is True
    assert row.missing_terms == ()


def test_a_death_scores_zero_for_that_term_not_thirty(config):
    row = score(config, "a1", SECTION_23, observed_at=NOW, mortality_observed=False,
                mortality_source="ipd_admissions.disposition")
    assert row.reward_total == 170
    assert row.complete is True, "observed to be false is still observed"


def test_unknown_is_not_false(config):
    """The distinction the whole layer turns on.

    An unobserved term is excluded and flagged; a term observed to be false scores zero and
    the episode stays complete. Collapsing the two would let a silent pipeline failure look
    like a run of bad outcomes.
    """
    unknown = score(config, "a1", {**SECTION_23, "patient_stabilised": None}, observed_at=NOW)
    false = score(config, "a1", {**SECTION_23, "patient_stabilised": False}, observed_at=NOW)

    assert unknown.reward_total == false.reward_total == 130
    assert "patient_stabilised" in unknown.missing_terms
    assert "patient_stabilised" not in false.missing_terms


def test_a_misspelt_term_raises(config):
    """Silently dropping it would remove points from the objective and nothing would say so."""
    with pytest.raises(KeyError, match="not in reward.yaml"):
        score(config, "a1", {"patient_stabalised": True}, observed_at=NOW)


# ---------------------------------------------------------------------------------------
# Deferred observation
# ---------------------------------------------------------------------------------------


def test_observation_is_due_after_the_horizon(config):
    pending = pending_for(config, "a1", closed_at=NOW)
    assert pending.horizon_hours == 4.0
    assert pending.due_at == NOW + timedelta(hours=4)
    assert not pending.is_due(NOW + timedelta(hours=3, minutes=59))
    assert pending.is_due(pending.due_at)


def test_due_filters_the_queue(config):
    early = pending_for(config, "a1", closed_at=NOW - timedelta(hours=5))
    late = pending_for(config, "a2", closed_at=NOW)
    assert due((early, late), NOW) == (early,)


# ---------------------------------------------------------------------------------------
# Episodes — section 21
# ---------------------------------------------------------------------------------------


def _step(reward: float, won: bool = True, complete: bool = True, cost: float = 20.0) -> Step:
    return Step("a", 3, won, bid=100.0, utility=150.0, cost=cost, reward=reward, complete=complete)


def test_episode_discounts_across_the_shift(config):
    """Section 21: the agent maximises the discounted return, not this auction's reward."""
    episode = build_episode(config, AgentKind.ER, "2026-08-07:evening",
                            [_step(100), _step(100), _step(100)])
    assert episode.total_reward == 300
    assert episode.discounted_return == pytest.approx(100 + 99 + 98.01)
    assert episode.discounted_return < episode.total_reward


def test_burn_rate_is_realised_not_predicted(config):
    """The health metric of the whole mechanism, measured from what was actually spent."""
    episode = build_episode(config, AgentKind.ER, "s", [_step(100, cost=20.0)] * 6)
    assert episode.spend == 120.0
    assert episode.burn_rate(125.4) == pytest.approx(0.957, abs=0.01)


def test_one_unscored_auction_makes_the_whole_episode_unusable(config):
    """Dropping the unscored step would tell the policy a shift with an unknown death was fine."""
    episode = build_episode(config, AgentKind.ER, "s",
                            [_step(100), _step(100, complete=False), _step(100)])
    assert episode.complete is False
    assert trainable([episode]) == ()


def test_no_episode_is_trainable_today(config):
    """The clearest statement of what F-01 costs: not a degraded training set, an empty one.

    Every real episode contains at least one auction whose ``no_mortality`` is unobserved, so
    every episode is incomplete, so nothing is trainable. Until a disposition field exists,
    this test should keep passing — and when it starts failing, that is the signal that RL
    training has become possible.
    """
    row = score(config, "a1", SECTION_23, observed_at=NOW)
    episode = build_episode(
        config, AgentKind.ER, "s",
        [_step(row.reward_total, complete=row.complete)],
    )
    assert trainable([episode]) == ()


def test_reward_terms_are_reported_unsigned(config):
    """They are the objective function; nobody has signed them."""
    assert config.unsigned["reward.terms"] == "assumed_pending_clinical_signoff"
