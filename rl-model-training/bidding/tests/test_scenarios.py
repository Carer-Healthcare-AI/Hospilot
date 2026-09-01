"""Scenario files — changing inputs without changing code.

The tests that matter here are the ones proving the pipeline is *wired*: that moving an input
moves the output, in the right direction, by an amount that traces back to the change. A
system that printed Appendix C's numbers regardless would pass none of them.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from allocation.config import load_config
from allocation.contracts import AgentKind, ComponentName
from allocation.ingest import fixtures as fx
from allocation.ingest.scenarios import ScenarioError, load_scenario, parse_moment
from allocation.trigger.runtime import run_allocation

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


def _run(config, source, candidates):
    return run_allocation(
        config=config, source=source, candidates=candidates, now=fx.NOW,
        query="one limited ICU bed",
    )


def _totals(run):
    return {
        data.candidate.agent: run.utilities[cid].total
        for cid, data in run.snapshot.patients.items()
    }


@pytest.fixture
def baseline(config):
    return _run(config, fx.FixtureDataSource(), fx.CANDIDATES)


def _with(**kw):
    """A source with one patient field replaced."""
    def build(cid, **fields):
        patients = dict(fx.PATIENT_DATA)
        patients[cid] = replace(patients[cid], **fields)
        return fx.FixtureDataSource(patients=patients)
    return build


# -- time parsing -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,delta",
    [
        ("-55m", timedelta(minutes=-55)),
        ("+2h", timedelta(hours=2)),
        ("-1d", timedelta(days=-1)),
        ("0m", timedelta()),
    ],
)
def test_relative_times_are_resolved_against_now(text, delta):
    assert parse_moment(text, fx.NOW, "x") == fx.NOW + delta


def test_an_unreadable_time_names_the_key_it_came_from():
    with pytest.raises(ScenarioError, match="ward.vitals"):
        parse_moment("half an hour ago", fx.NOW, "ward.vitals[0].at")


# -- the shipped scenario -----------------------------------------------------------------


def test_ward_crash_scenario_loads_and_runs(config):
    source, candidates, description = load_scenario(SCENARIOS / "ward_crash.yaml", fx.NOW)
    assert len(candidates) == 3
    assert "Ward" in description

    run = _run(config, source, candidates)
    totals = _totals(run)

    # The whole point of the file: Ward now values the bed most.
    assert totals[AgentKind.WARD] > totals[AgentKind.ER]


def test_the_sickest_patient_now_wins_under_a_common_base(config):
    """F-25, resolved by RL-Steps section 4's common Base.

    Under AGENT_BUDGET v0.3's derived Base, Ward got 23.4 points against a clinical ceiling
    of 111.9 — its maximum affordable bid was 79, so the *budget* was its real ceiling and it
    lost with the sickest patient in the hospital. A common Base of 700 gives it 910, the
    affordability guard stops binding, and clinical need decides the allocation again.

    This is the strongest argument for the common Base, and it is why this test asserts the
    win rather than the flag.
    """
    source, candidates, _ = load_scenario(SCENARIOS / "ward_crash.yaml", fx.NOW)
    run = _run(config, source, candidates)

    ward = run.outcome.result.positions[AgentKind.WARD]
    er = run.outcome.result.positions[AgentKind.ER]

    assert ward.utility > er.utility, "the scenario must make Ward the sickest"
    assert run.winner is AgentKind.WARD
    assert ward.current_bid <= ward.ceiling


def test_a_derived_base_still_lets_the_budget_override_clinical_need(config):
    """The F-25 failure mode, pinned so switching modes cannot reintroduce it silently."""
    from dataclasses import replace

    derived = replace(
        config,
        budget={**config.budget, "base": {**config.budget["base"], "mode": "derived"}},
    )
    source, candidates, _ = load_scenario(SCENARIOS / "ward_crash.yaml", fx.NOW)
    run = _run(derived, source, candidates)

    ward = run.outcome.result.positions[AgentKind.WARD]
    assert ward.utility > run.outcome.result.positions[AgentKind.ER].utility
    assert run.winner is AgentKind.ER
    assert ward.current_bid < ward.ceiling, "Ward never reached its clinical ceiling"


def test_scenario_reproduces_what_an_in_process_perturbation_produces(config):
    """The YAML path and the object path must agree, or the file is not testing the engine."""
    crash = fx.WARD_VITALS + (
        replace(fx.WARD_VITALS[-1], recorded_at=fx.NOW - timedelta(minutes=2),
                temperature=39.1, pulse=132, bp_systolic=84, spo2=85,
                respiratory_rate=32, gcs=13, is_critical=True),
    )
    in_process = _run(config, _with()("Ward-Patient-C", vitals=crash), fx.CANDIDATES)
    source, candidates, _ = load_scenario(SCENARIOS / "ward_crash.yaml", fx.NOW)
    from_file = _run(config, source, candidates)

    assert _totals(from_file)[AgentKind.WARD] == pytest.approx(
        _totals(in_process)[AgentKind.WARD], rel=1e-6
    )


# -- proof the pipeline is wired ----------------------------------------------------------


def test_worse_physiology_raises_utility(config, baseline):
    crash = fx.WARD_VITALS + (
        replace(fx.WARD_VITALS[-1], recorded_at=fx.NOW - timedelta(minutes=2),
                spo2=85, respiratory_rate=32, gcs=13, pulse=132, bp_systolic=84),
    )
    run = _run(config, _with()("Ward-Patient-C", vitals=crash), fx.CANDIDATES)
    assert _totals(run)[AgentKind.WARD] > _totals(baseline)[AgentKind.WARD]


def test_better_physiology_lowers_utility(config, baseline):
    settled = fx.ER_VITALS[:1] + (
        replace(fx.ER_VITALS[-1], spo2=98, respiratory_rate=16, gcs=15, pulse=78,
                bp_systolic=124, temperature=37.0, is_critical=False, on_oxygen=False),
    )
    run = _run(config, _with()("ER-Patient-A", vitals=settled), fx.CANDIDATES)
    assert _totals(run)[AgentKind.ER] < _totals(baseline)[AgentKind.ER]


def test_losing_an_alternative_raises_utility(config, baseline):
    """Alternative carries a negative cap: no fallback unit means less penalty, so more points."""
    run = _run(config, _with()("Ward-Patient-C", best_alternative_unit=None), fx.CANDIDATES)
    assert _totals(run)[AgentKind.WARD] > _totals(baseline)[AgentKind.WARD]


def test_freeing_icu_beds_lowers_every_budget(config, baseline):
    """One scarcity value per auction — it moves all budgets together and changes no winner.

    That is still true across *agents*, which is what this asserts. It stopped being true
    across bed types at D-4: the value comes from the occupancy of the unit being auctioned.
    """
    source = fx.FixtureDataSource(hospital=replace(fx.HOSPITAL_STATE, unit_occupied_beds=14))
    run = _run(config, source, fx.CANDIDATES)

    for agent, state in run.opening_budgets.items():
        assert state.budget_total < baseline.opening_budgets[agent].budget_total
    assert run.winner is baseline.winner


def test_an_absent_input_lowers_coverage_rather_than_scoring_zero(config, baseline):
    """The three-state rule, end to end. Dropping labs must not read as 'labs were normal'."""
    run = _run(config, _with()("ER-Patient-A", labs=()), fx.CANDIDATES)

    def coverage(r):
        breakdown = r.utilities["ER-Patient-A"]
        return next(
            c.coverage for c in breakdown.components
            if c.component is ComponentName.CLINICAL_BENEFIT
        )

    assert coverage(run) < coverage(baseline)
    assert _totals(run)[AgentKind.ER] > 0, "an absent lab must not zero the component"


# -- config is live too -------------------------------------------------------------------


def test_changing_a_cap_changes_the_points_and_the_version(tmp_path, config, baseline):
    """The caps table is the calibration, and it is content-addressed.

    Doubling one cap must move exactly that component's points and nothing else's, and must
    produce a different ``caps_version`` — otherwise two incompatible scores could be logged
    under one version and neither would be re-derivable.

    Edits ``caps_icu_bed.yaml`` specifically: caps are per resource type, and this run
    auctions an ICU bed. Editing another bed's table must leave this run untouched, which is
    what ``test_editing_another_beds_caps_leaves_this_run_alone`` covers.
    """
    alt = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[1] / "allocation" / "config", alt)
    caps = alt / "caps_icu_bed.yaml"
    text = caps.read_text(encoding="utf-8")
    assert "urgency:\n    cap: 40" in text
    caps.write_text(text.replace("urgency:\n    cap: 40", "urgency:\n    cap: 80"), "utf-8")

    bumped = _run(load_config(alt), fx.FixtureDataSource(), fx.CANDIDATES)

    def urgency(r):
        return next(
            c.points for c in r.utilities["ER-Patient-A"].components
            if c.component is ComponentName.URGENCY
        )

    assert urgency(bumped) == pytest.approx(2 * urgency(baseline), rel=1e-9)
    assert bumped.outcome.result.caps_version != baseline.outcome.result.caps_version


def test_editing_another_beds_caps_leaves_this_run_alone(tmp_path, baseline):
    """``caps_version`` must hash the file actually used, not the whole config directory.

    If it hashed everything, a ward-bed cap edit would restamp every ICU-bed row with a new
    version describing a change that did not affect it — and B.13 cap fitting, which reads
    those rows back, would be re-deriving against the wrong table.
    """
    alt = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[1] / "allocation" / "config", alt)
    caps = alt / "caps_ward_bed.yaml"
    text = caps.read_text(encoding="utf-8")
    caps.write_text(text.replace("urgency:\n    cap: 40", "urgency:\n    cap: 80"), "utf-8")

    run = _run(load_config(alt), fx.FixtureDataSource(), fx.CANDIDATES)

    assert run.outcome.result.caps_version == baseline.outcome.result.caps_version
    # config_version hashes every file, so it *does* move — that is the point of having both.
    assert run.outcome.result.config_version != baseline.outcome.result.config_version


# -- a scenario cannot fail quietly -------------------------------------------------------


@pytest.mark.parametrize(
    "body,message",
    [
        ("description: x\n", "hospital"),
        ("hospital: {icu_total_beds: 20, icu_occupied_beds: 20}\n", "candidates"),
    ],
)
def test_an_incomplete_scenario_raises_with_the_missing_key(tmp_path, body, message):
    path = tmp_path / "bad.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ScenarioError, match=message):
        load_scenario(path, fx.NOW)


def test_an_unknown_agent_lists_the_valid_ones(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "hospital: {icu_total_beds: 20, icu_occupied_beds: 20}\n"
        "candidates:\n"
        "  - {candidate_id: X, agent: radiology, arrived_at: -5m}\n",
        encoding="utf-8",
    )
    with pytest.raises(ScenarioError, match="radiology"):
        load_scenario(path, fx.NOW)


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(ScenarioError, match="no scenario file"):
        load_scenario(tmp_path / "nope.yaml", fx.NOW)


# -- hospital state is unit-scoped, and the icu_* spelling still parses --------------------


def _hospital_from(tmp_path, hospital: str, unit: str = "icu"):
    """The HospitalState a scenario whose ``hospital:`` block reads ``hospital`` parses to.

    ``unit`` is the unit to read back, since a source now answers per unit rather than
    holding one state.
    """
    path = tmp_path / "s.yaml"
    path.write_text(
        f"hospital: {{{hospital}}}\n"
        "candidates:\n"
        "  - {candidate_id: X, agent: er, arrived_at: -5m}\n",
        encoding="utf-8",
    )
    source, _candidates, _description = load_scenario(path, fx.NOW)
    return asyncio.run(source.hospital_state(unit, fx.NOW))


def test_legacy_icu_keys_parse_as_the_icu_unit(tmp_path):
    """One release of compatibility: existing scenario files and API callers must not break."""
    state = _hospital_from(
        tmp_path, "icu_total_beds: 20, icu_occupied_beds: 19, predicted_icu_demand_4h: 4.0"
    )
    assert state.unit == "icu"
    assert (state.unit_total_beds, state.unit_occupied_beds) == (20, 19)
    assert state.predicted_demand_4h == 4.0


def test_unit_scoped_keys_parse_for_a_non_icu_unit(tmp_path):
    state = _hospital_from(
        tmp_path, "unit: ward, unit_total_beds: 40, unit_occupied_beds: 24", unit="ward"
    )
    assert state.unit == "ward"
    assert state.occupancy == pytest.approx(0.60)
    assert state.predicted_demand_4h is None


def test_the_new_spelling_without_a_unit_refuses_to_guess(tmp_path):
    """No default unit — scoring one unit's beds against another's caps is the whole hazard."""
    with pytest.raises(ScenarioError, match="unit"):
        _hospital_from(tmp_path, "unit_total_beds: 20, unit_occupied_beds: 19")


# -- Step 12 · a scenario describes as many units as it wants to auction -------------------


def _multi_unit(tmp_path, hospital: str):
    """A scenario whose ``hospital:`` block is the (already indented) YAML in ``hospital``."""
    path = tmp_path / "multi.yaml"
    path.write_text(
        f"hospital:\n{hospital}"
        "candidates:\n"
        "  - {candidate_id: X, agent: er, arrived_at: -5m}\n",
        encoding="utf-8",
    )
    return load_scenario(path, fx.NOW)[0]


_TWO_UNITS = (
    "  boarding_count: 7\n"
    "  lwbs_risk: 0.42\n"
    "  units:\n"
    "    - {unit: icu,  unit_total_beds: 20, unit_occupied_beds: 20, active_isolation_cases: 2}\n"
    "    - {unit: ward, unit_total_beds: 40, unit_occupied_beds: 24, active_isolation_cases: 3}\n"
)


def test_a_scenario_can_describe_two_units_at_once(tmp_path):
    """Required to exercise the unit argument at all: one file, two occupancies."""
    source = _multi_unit(tmp_path, _TWO_UNITS)

    icu = asyncio.run(source.hospital_state("icu", fx.NOW))
    ward = asyncio.run(source.hospital_state("ward", fx.NOW))

    assert icu.occupancy == pytest.approx(1.00)
    assert ward.occupancy == pytest.approx(0.60)
    assert (icu.active_isolation_cases, ward.active_isolation_cases) == (2, 3)


def test_the_hospital_wide_fields_are_stated_once_and_reach_every_unit(tmp_path):
    """ED boarding is a fact about the ED's queue, not about the bed being auctioned.

    Repeating it per unit would be six chances for two units to disagree about one number.
    """
    source = _multi_unit(tmp_path, _TWO_UNITS)

    for unit in ("icu", "ward"):
        state = asyncio.run(source.hospital_state(unit, fx.NOW))
        assert state.boarding_count == 7
        assert state.lwbs_risk == pytest.approx(0.42)


def test_a_per_unit_field_beside_units_is_refused_and_named(tmp_path):
    """Sharing ``unit_occupied_beds`` across units is the exact bug Step 12 removes.

    So is sharing a demand forecast: ``predicted_demand_4h`` is *this unit's* demand.
    """
    with pytest.raises(ScenarioError, match="predicted_demand_4h"):
        _multi_unit(
            tmp_path,
            "  predicted_demand_4h: 4.0\n"
            "  units:\n"
            "    - {unit: icu, unit_total_beds: 20, unit_occupied_beds: 20}\n",
        )


def test_one_unit_described_twice_is_refused(tmp_path):
    """Two occupancies for one unit at one instant is not a hospital state."""
    with pytest.raises(ScenarioError, match="described twice"):
        _multi_unit(
            tmp_path,
            "  units:\n"
            "    - {unit: ward, unit_total_beds: 40, unit_occupied_beds: 24}\n"
            "    - {unit: ward, unit_total_beds: 40, unit_occupied_beds: 30}\n",
        )


def test_an_empty_units_list_is_refused(tmp_path):
    with pytest.raises(ScenarioError, match="non-empty"):
        _multi_unit(tmp_path, "  units: []\n")


def test_the_step_down_scenario_prices_two_units_from_one_file(config):
    """``scenarios/step_down.yaml`` documents a decomposition; this pins it.

    The file's header states five component deltas with a cause for each. A header that drifts
    from the run is worse than no header, because it is the thing a reader trusts instead of
    checking.
    """
    from allocation.contracts import ResourceType
    from allocation.profiles.registry import REGISTRY

    source, candidates, _ = load_scenario(SCENARIOS / "step_down.yaml", fx.NOW)

    runs = {
        resource_type: run_allocation(
            config=config, source=source, candidates=candidates, now=fx.NOW,
            profile=REGISTRY.get(resource_type),
        )
        for resource_type in (ResourceType.ICU_BED, ResourceType.WARD_BED)
    }
    icu, ward = runs[ResourceType.ICU_BED], runs[ResourceType.WARD_BED]

    # The two occupancies the file exists to demonstrate, out of one document.
    assert icu.snapshot.hospital.occupancy == pytest.approx(1.00)
    assert ward.snapshot.hospital.occupancy == pytest.approx(0.60)
    assert icu.outcome.result.reserve_price == pytest.approx(97.9, abs=0.1)
    assert ward.outcome.result.reserve_price == pytest.approx(52.8, abs=0.1)

    def points(run, component):
        breakdown = run.utilities["Ward-Patient-C"]
        return next(c for c in breakdown.components if c.component is component)

    assert icu.utilities["Ward-Patient-C"].total == pytest.approx(48.6, abs=0.1)
    assert ward.utilities["Ward-Patient-C"].total == pytest.approx(56.4, abs=0.1)

    # Step 12's own contribution: resource stress is the ward's, not the ICU's.
    assert points(icu, ComponentName.RESOURCE_STRESS).points == pytest.approx(-8.20, abs=0.05)
    assert points(ward, ComponentName.RESOURCE_STRESS).points == pytest.approx(-5.65, abs=0.05)

    # And the cost of the undefined ward benefit table (C-3), in coverage rather than points.
    assert points(icu, ComponentName.CLINICAL_BENEFIT).coverage == pytest.approx(0.90, abs=0.01)
    assert points(ward, ComponentName.CLINICAL_BENEFIT).coverage == pytest.approx(0.65, abs=0.01)


def test_a_single_unit_scenario_cannot_be_auctioned_another_units_bed(tmp_path, config):
    """The honest failure. ``ward_crash.yaml`` describes the ICU, so it prices ICU beds.

    Answering a ward-bed auction from it would have to serve ICU's 20/20, which is how every
    auction came to report 100% occupancy in the first place.
    """
    from allocation.contracts import ResourceType
    from allocation.profiles.registry import REGISTRY

    source, candidates, _ = load_scenario(SCENARIOS / "ward_crash.yaml", fx.NOW)

    with pytest.raises(ValueError, match="no fixture hospital state for unit 'ward'"):
        run_allocation(
            config=config, source=source, candidates=candidates, now=fx.NOW,
            profile=REGISTRY.get(ResourceType.WARD_BED),
        )
