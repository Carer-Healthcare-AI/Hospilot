"""The whole utility stack against END_TO_END Appendix C.

Appendix C publishes every raw row and every intermediate value, which makes it the only
validation the framework has. It is used here at two levels:

**Component level** — where Appendix C's arithmetic follows from its own declared inputs, the
computed points must match to within rounding. The reference rounds intermediates to three
decimals (e.g. SpO2 deficit 0.2727 printed as 0.273), so a tolerance of 0.1 point is used
rather than exact equality.

**End-to-end** — the totals are recomputed from C.1's declared shared state through every
component. They do **not** reproduce C.5 exactly, and the deltas are asserted explicitly with
their cause. Silently matching C.5 would mean hard-coding Appendix C's internal
inconsistencies into the engine.

::

                 C.5      here     delta   cause
    ER          107.1     107.4     +0.3   Throughput +1.0, Alternative -0.6
    OT           34.2      35.1     +0.9   Throughput +1.0
    Ward         45.7      48.6     +2.9   Throughput +2.9, Financial +0.15

    ranking     ER > Ward > OT      preserved

The three causes, all recorded in BUILD_SPEC as F-20 and F-21:

1. **Downstream discharges.** C.1 declares expected discharges over 4 h as **1**, and Resource
   Stress in the same appendix reads it as 1. But every Throughput block computes Downstream as
   ``1 - 3/5 = 0.400``, which needs 3. C.1 wins here, giving 0.800 and +1.0 point to each
   bidder.
2. **Ward's queue impact.** C.4 uses 0.300 while ER and OT use 0.700 from the same
   ``/er/boarding`` count of 7. D.4 defines Queue Impact hospital-wide with no per-agent
   variant, so every bidder gets 0.700 — +2.0 points for Ward.
3. **ER's alternative quality.** C.0 declares 0.40. Quality is
   ``|needs & capability| / |needs|``, so with a four-need profile the reachable values are
   0.25 / 0.50 / 0.75. The capability vector gives 0.50, making ER's penalty 0.6 points
   deeper.

None of these changes the ranking, and the ranking is the finding that matters: computed from
real data, OT falls from second to third, exactly as Appendix C.5 reports.
"""

from __future__ import annotations

import pytest

from allocation.contracts import ComponentName
from allocation.ingest.fixtures import ER_CANDIDATE, OT_CANDIDATE, WARD_CANDIDATE
from allocation.utility import ceiling_for

# Tolerance covering the reference's 3-decimal intermediate rounding.
TOL = 0.1


def _points(breakdown, component: ComponentName) -> float:
    return next(c.points for c in breakdown.components if c.component is component)


# ---------------------------------------------------------------------------------------
# Component level — where Appendix C's arithmetic is self-consistent
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "component", "expected", "reference"),
    [
        (ER_CANDIDATE, ComponentName.CLINICAL_BENEFIT, 45.4, "C.2 — 60 x 0.7559, coverage 90%"),
        (ER_CANDIDATE, ComponentName.URGENCY, 23.7, "C.2 — 40 x 0.5934, coverage 85%"),
        (ER_CANDIDATE, ComponentName.WAITING, 15.4, "C.2 — 25 x 0.615"),
        (ER_CANDIDATE, ComponentName.OPERATIONAL, 11.2, "C.2 — 20 x 0.560"),
        (ER_CANDIDATE, ComponentName.FINANCIAL, 5.3, "C.2 — 10 x 0.528"),
        (OT_CANDIDATE, ComponentName.CLINICAL_BENEFIT, 7.5, "C.3 — 60 x 0.125"),
        (OT_CANDIDATE, ComponentName.URGENCY, 7.7, "C.3 — 40 x 0.191"),
        (OT_CANDIDATE, ComponentName.WAITING, 4.6, "C.3 — P(no PACU) substitution"),
        (OT_CANDIDATE, ComponentName.OPERATIONAL, 12.0, "C.3 — 3 cases at risk / 5"),
        (OT_CANDIDATE, ComponentName.FINANCIAL, 10.0, "C.3 — every term clamps to 1.0"),
        (OT_CANDIDATE, ComponentName.ALTERNATIVE, -13.0, "C.3 — PACU 1.00 x 0.65"),
        (WARD_CANDIDATE, ComponentName.CLINICAL_BENEFIT, 21.7, "C.4 — 60 x 0.361"),
        (WARD_CANDIDATE, ComponentName.URGENCY, 10.6, "C.4 — 40 x 0.265"),
        (WARD_CANDIDATE, ComponentName.WAITING, 3.6, "C.4 — 25 x 0.143"),
        (WARD_CANDIDATE, ComponentName.OPERATIONAL, 13.8, "C.4 — 20 x 0.688"),
        (WARD_CANDIDATE, ComponentName.ALTERNATIVE, -14.0, "C.4 — HDU 1.00 x 0.70"),
    ],
)
def test_component_matches_appendix_c(engine, snapshot, candidate, component, expected, reference):
    breakdown = engine.score(candidate, snapshot, snapshot.taken_at)
    assert _points(breakdown, component) == pytest.approx(expected, abs=TOL), reference


def test_resource_stress_is_identical_for_every_bidder(engine, snapshot):
    """D.8 — none of its factors reads the patient, so it cannot change who wins.

    If this ever diverges between bidders, a patient-dependent term has leaked into a
    component that is supposed to describe the hospital.
    """
    values = {
        c.candidate_id: _points(engine.score(c, snapshot, snapshot.taken_at),
                                ComponentName.RESOURCE_STRESS)
        for c in (ER_CANDIDATE, OT_CANDIDATE, WARD_CANDIDATE)
    }
    assert len(set(round(v, 6) for v in values.values())) == 1
    assert all(v == pytest.approx(-8.2, abs=TOL) for v in values.values()), "C.1 publishes -8.2"


def test_a_missing_forecast_is_absent_and_a_renamed_field_is_not(config, snapshot):
    """C-6, both halves. A forecast nobody published is absent; a field nobody renamed is not.

    ``_demand`` used to read its two fields through ``getattr(state, name, None)``, which made
    the two indistinguishable: rename ``predicted_demand_4h`` and every patient in every
    auction would quietly score the forecast as absent, with nothing but an unexplained
    coverage drop to show for it. The first assertion is the behaviour that must survive
    tightening; the second is the failure that must now be loud.
    """
    from dataclasses import replace

    from allocation.contracts import AgentKind
    from allocation.utility.components.resource_stress import ResourceStress

    component = ResourceStress(config)

    no_forecast = replace(snapshot, hospital=replace(snapshot.hospital, predicted_demand_4h=None))
    demand = next(
        f for f in component.score(ER_CANDIDATE, no_forecast, AgentKind.ER).factors
        if f.name == "demand_pressure"
    )
    assert not demand.signal.present
    assert "forecast unavailable" in demand.signal.note

    class RenamedField:
        """``HospitalState`` after someone renames the forecast field and misses this reader."""

        unit = "icu"
        occupancy = 1.0
        isolation_pressure = 0.1
        expected_discharges_4h = 1.0

    with pytest.raises(AttributeError, match="predicted_demand_4h"):
        component.score(
            ER_CANDIDATE, replace(snapshot, hospital=RenamedField()), AgentKind.ER
        )


# ---------------------------------------------------------------------------------------
# End-to-end — recomputed reference values, with the deltas from C.5 asserted
# ---------------------------------------------------------------------------------------

#: Recomputed from C.1's declared state through D.1-D.8. See the module docstring for the
#: three causes of divergence from C.5's 107.1 / 34.2 / 45.7.
EXPECTED_TOTALS = {
    "ER-Patient-A": 107.4,
    "OT-Patient-B": 35.1,
    "Ward-Patient-C": 48.6,
}

APPENDIX_C5 = {
    "ER-Patient-A": 107.1,
    "OT-Patient-B": 34.2,
    "Ward-Patient-C": 45.7,
}


@pytest.mark.parametrize("candidate", [ER_CANDIDATE, OT_CANDIDATE, WARD_CANDIDATE])
def test_total_utility(engine, snapshot, candidate):
    breakdown = engine.score(candidate, snapshot, snapshot.taken_at)
    assert breakdown.total == pytest.approx(EXPECTED_TOTALS[candidate.candidate_id], abs=TOL)


@pytest.mark.parametrize("candidate", [ER_CANDIDATE, OT_CANDIDATE, WARD_CANDIDATE])
def test_divergence_from_appendix_c5_stays_bounded(engine, snapshot, candidate):
    """The deltas are known and small. A larger one means something else has changed."""
    breakdown = engine.score(candidate, snapshot, snapshot.taken_at)
    delta = breakdown.total - APPENDIX_C5[candidate.candidate_id]
    assert 0.0 <= delta <= 3.0, f"unexpected divergence of {delta:+.2f} from Appendix C.5"


def test_ranking_matches_appendix_c5(engine, snapshot):
    """The finding that survives the deltas: OT falls from second place to third.

    C.5: "Second and third place swap. OT falls from 117 to 34.2 because two of its
    components collapse under a real derivation." RL-Steps ranked ER > OT > Ward; computed
    from real data the order is ER > Ward > OT, and the spread widens from 1.6x to ~3x.
    """
    scores = {
        c.candidate_id: engine.score(c, snapshot, snapshot.taken_at).total
        for c in (ER_CANDIDATE, OT_CANDIDATE, WARD_CANDIDATE)
    }
    order = sorted(scores, key=lambda k: scores[k], reverse=True)
    assert order == ["ER-Patient-A", "Ward-Patient-C", "OT-Patient-B"]
    assert scores["ER-Patient-A"] / scores["OT-Patient-B"] > 2.5


# ---------------------------------------------------------------------------------------
# Coverage — the rule that a missing input is dropped, never scored zero
# ---------------------------------------------------------------------------------------


def test_clinical_benefit_coverage_is_ninety_percent(engine, snapshot):
    """Age/comorbidity (.10) has no source for any patient: no DOB in hospilot.patients."""
    for candidate in (ER_CANDIDATE, OT_CANDIDATE, WARD_CANDIDATE):
        breakdown = engine.score(candidate, snapshot, snapshot.taken_at)
        coverage = breakdown.coverage[ComponentName.CLINICAL_BENEFIT]
        assert coverage == pytest.approx(0.90), f"{candidate.candidate_id} — C.2 reports 90%"


def test_urgency_coverage_is_eighty_five_percent(engine, snapshot):
    """Time-to-critical (.15) has no model — B.5, buildable today, not built."""
    for candidate in (ER_CANDIDATE, OT_CANDIDATE, WARD_CANDIDATE):
        breakdown = engine.score(candidate, snapshot, snapshot.taken_at)
        coverage = breakdown.coverage[ComponentName.URGENCY]
        assert coverage == pytest.approx(0.85), f"{candidate.candidate_id} — C.2 reports 85%"


def test_absent_factor_does_not_score_zero(engine, snapshot):
    """The rule from D.0, checked structurally rather than by inspection.

    Every absent factor must carry a None value. A 0.0 would say "this patient is fine" about
    something nobody measured, and would rank an untested patient above a tested one.
    """
    breakdown = engine.score(ER_CANDIDATE, snapshot, snapshot.taken_at)
    absent = [
        f for component in breakdown.components for f in component.factors if not f.present
    ]
    assert absent, "expected at least age/comorbidity and time-to-critical to be absent"
    assert all(f.signal.value is None for f in absent)
    assert all(f.signal.note for f in absent), "every absence must explain itself"


# ---------------------------------------------------------------------------------------
# Ceiling
# ---------------------------------------------------------------------------------------


def test_ceiling_falls_back_to_utility(engine, snapshot):
    """D.9 — Ceiling = U until B.9 exists. Conservative: it can only understate willingness."""
    breakdown = engine.score(ER_CANDIDATE, snapshot, snapshot.taken_at)
    ceiling = ceiling_for(breakdown)
    assert ceiling.value == pytest.approx(breakdown.total)
    assert ceiling.uplift == 0.0
    assert "B.9" in ceiling.note


def test_versions_are_stamped(engine, snapshot):
    """Nothing is stored unversioned: budgets are denominated in these caps."""
    breakdown = engine.score(ER_CANDIDATE, snapshot, snapshot.taken_at)
    assert breakdown.caps_version and breakdown.config_version
    assert breakdown.caps_version == snapshot.caps_version
