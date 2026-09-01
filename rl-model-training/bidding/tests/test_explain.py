"""The derivation printout.

An explanation that can drift from the engine is worse than none — someone hand-checks it,
it reconciles, and the thing they verified was the renderer. So the tests here are mostly
about **agreement with the run**, not about formatting.
"""

from __future__ import annotations

import re

import pytest

from allocation.explain import OWN_FORMULA, component_lines, explain
from allocation.ingest import fixtures as fx
from allocation.trigger.runtime import run_allocation

PRODUCT_COMPONENTS = set(OWN_FORMULA)


@pytest.fixture
def run(config):
    return run_allocation(
        config=config, source=fx.FixtureDataSource(), candidates=fx.CANDIDATES,
        now=fx.NOW, query="one limited ICU bed",
    )


@pytest.fixture
def text(run, config):
    return explain(run, config)


# -- agreement with the engine ------------------------------------------------------------


def test_every_printed_points_line_matches_the_component(run):
    """The renderer must never state a number the engine did not produce."""
    for breakdown in run.utilities.values():
        for result in breakdown.components:
            printed = [l for l in component_lines(result) if "POINTS" in l][0]
            value = float(re.search(r"=\s*([+-][\d.]+)$", printed).group(1))
            assert value == pytest.approx(result.points, abs=0.005)


def test_the_printed_totals_sum_to_the_utility(run, text):
    for cid, breakdown in run.utilities.items():
        assert f"{breakdown.total:.2f}" in text


def test_weighted_components_show_their_renormalisation(run):
    """Five of the eight follow D.0 and must show numerator, denominator and coverage."""
    shown = 0
    for breakdown in run.utilities.values():
        for result in breakdown.components:
            if result.component.value in PRODUCT_COMPONENTS:
                continue
            body = "\n".join(component_lines(result))
            assert "numerator" in body and "denominator" in body
            shown += 1
    assert shown, "no weighted component was rendered"


def test_product_components_are_not_explained_as_weighted_means(run):
    """The bug this guard exists for.

    ``waiting`` is ``clamp(P(det) x severity x (1 + delay) / max)``. Printing D.0's weighted
    mean over it showed ``normalised 0.7431`` above ``POINTS +25 x 0.6146`` — arithmetic that
    does not produce the number beneath it.

    ``operational`` is deliberately not asserted here. It is a per-agent rule (D.5) and so
    sits in :data:`OWN_FORMULA`, but it currently emits a *single* factor at weight 1.0 — for
    which the weighted mean and the rule are the same number. The suppression is numeric, so
    it correctly does not fire, and rendering the renormalisation there is accurate.
    """
    seen = set()
    for breakdown in run.utilities.values():
        for result in breakdown.components:
            if result.component.value not in {"waiting", "alternative"}:
                continue
            body = "\n".join(component_lines(result))
            assert "formula" in body
            assert "numerator" not in body
            assert "does not use it" in body
            seen.add(result.component.value)
    assert seen == {"waiting", "alternative"}


def test_the_suppression_is_computed_not_hardcoded(run):
    """Detection is by reconstruction mismatch, so a new component cannot be mis-explained.

    If the check were a name list, removing an entry would silently produce wrong arithmetic
    again. It is a numeric comparison against ``points / cap``, so the name map only supplies
    a human-readable formula string.
    """
    for breakdown in run.utilities.values():
        for result in breakdown.components:
            if not result.factors or result.cap == 0:
                continue
            present = [f for f in result.factors if f.present]
            if not present:
                continue
            weight = sum(f.weight for f in present)
            reconstructed = sum(
                f.weight * float(f.signal.value or 0.0) for f in present
            ) / weight
            actual = result.points / result.cap
            body = "\n".join(component_lines(result))
            agrees = abs(reconstructed - actual) <= 1e-4
            assert ("numerator" in body) is agrees


# -- absence is legible -------------------------------------------------------------------


def test_absent_factors_show_their_weight_and_reason(run):
    """Coverage as a percentage is not actionable; a named missing input is."""
    body = "\n".join(
        line
        for breakdown in run.utilities.values()
        for result in breakdown.components
        for line in component_lines(result)
    )
    assert "ABSENT" in body
    assert "B.5 not built" in body, "the time-to-critical gap should be named"
    assert "no DOB column" in body, "the comorbidity gap should be named"


def test_dropped_weights_are_shown_in_the_denominator(run):
    body = "\n".join(
        line
        for breakdown in run.utilities.values()
        for result in breakdown.components
        for line in component_lines(result)
    )
    assert "dropped)" in body


# -- the rest of the derivation -----------------------------------------------------------


@pytest.mark.parametrize(
    "heading",
    ["FULL DERIVATION", "UTILITY", "CEILING", "BUDGET", "BIDS", "SETTLEMENT", "UNSIGNED"],
)
def test_every_stage_is_explained(text, heading):
    assert heading in text


def test_bids_show_the_increment_arithmetic(text):
    """``Increment = alpha x (Ceiling - CurrentBid)``, section 6, with the guard visible."""
    assert "headroom" in text
    assert "increment" in text
    assert "guard limit" in text
    assert "max affordable" in text


def test_a_clamped_bid_is_distinguishable_from_a_chosen_one(run, config):
    """Which matters enormously when fitting anything to this log."""
    body = explain(run, config)
    assert "not binding" in body or "CLAMPED" in body


def test_each_budget_factor_shows_its_formula(text):
    """Four of the five have one. ``base`` does not, and saying so is the point."""
    assert "clamp(forecast / median30(forecast), 0.8, 1.3)" in text
    assert "clamp(1 + 0.3 x (occupancy - 0.85) / 0.15, 1.0, 1.3)" in text
    assert "NOT derived from anything" in text


def test_a_fallback_factor_is_distinguishable_from_a_computed_one(run, text):
    """1.00 computed and 1.00 fallen back are the same number and different facts.

    Without provenance a reader sees ``x 1.00 x ... x 1.00`` and reasonably concludes the
    model ran. Three of the five terms are inert today.
    """
    assert "no 30-day forecast history yet — F-18" in text
    assert "auction log not available — B.12" in text
    assert "/icu/occupancy" in text, "the one live factor should say so"


def test_factor_provenance_reaches_the_audit_row(run):
    """Stored, not just printed — otherwise it cannot be checked after the fact."""
    for row in run.bundle.budgets:
        assert set(row.factor_sources) == {
            "demand", "criticality", "fairness", "scarcity"
        }
        assert all(row.factor_sources.values())


def test_settlement_shows_all_four_cost_terms(text):
    assert "Cost = Bid x Contention x Outcome x Rate" in text
    assert "contention" in text


def test_unsigned_inputs_are_listed_at_the_end(run, text):
    """The reader should finish knowing how much of what they just checked is assumed."""
    for name in run.outcome.result.unsigned_rules:
        assert name in text


def test_versions_are_stamped_on_the_derivation(run, text):
    assert run.outcome.result.caps_version in text
    assert run.outcome.result.config_version in text
