"""What changes, and what must not, when the same patients are auctioned a different bed.

Alternative Availability (Step 8) and target-unit benefit (Step 9) are the two components that
became *relative to the resource*. Both were written when ICU was the only auctionable bed, so
both silently assumed the target unit. These tests pin the relative behaviour, and — just as
importantly — pin that the ICU-bed path is unchanged, because Appendix C reproduces through it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from allocation import profiles  # noqa: F401  — importing registers the family
from allocation.config import load_config
from allocation.contracts import AgentKind, ComponentName, ResourceType
from allocation.ingest import fixtures as fx
from allocation.profiles.registry import REGISTRY
from allocation.trigger.runtime import run_allocation


@pytest.fixture(scope="module")
def config():
    return load_config()


def _run(config, resource_type: ResourceType):
    return run_allocation(
        config, fx.FixtureDataSource(), fx.CANDIDATES, fx.NOW,
        profile=REGISTRY.get(resource_type),
    )


def _component(run, candidate_id: str, name: ComponentName):
    return next(
        c for c in run.utilities[candidate_id].components if c.component is name
    )


def _factor(component, name: str):
    return next(f for f in component.factors if f.name == name)


# -- Step 8 · Alternative Availability is relative to the unit being auctioned -------------


def test_the_icu_auction_is_unchanged_by_the_ladder(config):
    """Every alternative in Appendix C is below ICU, so all three are still de-escalations.

    This is the regression guard for the whole step: the ladder must not move the one
    auction the worked example pins.
    """
    run = _run(config, ResourceType.ICU_BED)
    points = {
        cid: _component(run, cid, ComponentName.ALTERNATIVE).points
        for cid in ("ER-Patient-A", "OT-Patient-B", "Ward-Patient-C")
    }
    assert points["OT-Patient-B"] == pytest.approx(-13.0)
    assert points["Ward-Patient-C"] == pytest.approx(-14.0)
    assert points["ER-Patient-A"] < 0


def test_a_unit_is_not_an_alternative_to_itself(config):
    """Ward-Patient-C's fallback is HDU. In an HDU auction that is the bed being auctioned.

    Scoring it would say "you have a good alternative — HDU" while auctioning HDU.
    """
    icu = _component(_run(config, ResourceType.ICU_BED), "Ward-Patient-C",
                     ComponentName.ALTERNATIVE)
    hdu = _component(_run(config, ResourceType.HDU_BED), "Ward-Patient-C",
                     ComponentName.ALTERNATIVE)

    assert icu.points == pytest.approx(-14.0)
    assert hdu.points == pytest.approx(0.0)
    assert "unit being auctioned" in _factor(hdu, "quality").signal.note


def test_an_escalation_is_not_scored_as_a_fallback(config):
    """Ward sits at the bottom of the ladder, so every alternative in Appendix C is above it.

    The wrong reading is the one this step exists to prevent: quality 1.0 on a scarcer, better
    bed, penalising a patient for being sick enough to need more than the bed on offer.
    """
    run = _run(config, ResourceType.WARD_BED)
    for cid in ("ER-Patient-A", "OT-Patient-B", "Ward-Patient-C"):
        alternative = _component(run, cid, ComponentName.ALTERNATIVE)
        assert alternative.points == pytest.approx(0.0), cid
        assert "no fallback below ward" in _factor(alternative, "quality").signal.note


def test_dropping_the_penalty_raises_the_utility(config):
    """Alternative is a negative term, so removing it must move the total up, not down."""
    icu = _run(config, ResourceType.ICU_BED).utilities["Ward-Patient-C"].total
    hdu = _run(config, ResourceType.HDU_BED).utilities["Ward-Patient-C"].total
    assert hdu > icu


def test_a_zero_here_is_a_finding_not_an_absence(config):
    """"We looked and there is nothing below this unit" is a real 0, not a missing input.

    If it degraded to absent, the component would be dropped and the other seven
    renormalised — which would quietly *raise* the score instead of recording a fact.
    """
    alternative = _component(
        _run(config, ResourceType.WARD_BED), "OT-Patient-B", ComponentName.ALTERNATIVE
    )
    assert alternative.coverage == 1.0
    assert _factor(alternative, "quality").signal.present


# -- Step 9 · the benefit question is per resource, and inverts ----------------------------


def test_icu_benefit_still_scores_appendix_c(config):
    run = _run(config, ResourceType.ICU_BED)
    benefit = _factor(
        _component(run, "ER-Patient-A", ComponentName.CLINICAL_BENEFIT), "unit_benefit"
    )
    assert benefit.signal.value == pytest.approx(0.85)


@pytest.mark.parametrize(
    "resource_type",
    [r for r in ResourceType if r is not ResourceType.ICU_BED],
)
def test_every_other_bed_is_absent_rather_than_reusing_icus_answers(config, resource_type):
    """The question inverts, so ICU's numbers are not a starting point — they are wrong.

    Absent costs coverage and is visible. A copied table would produce a confident number
    wrong in a direction nothing downstream could detect.
    """
    run = _run(config, resource_type)
    component = _component(run, "ER-Patient-A", ComponentName.CLINICAL_BENEFIT)
    benefit = _factor(component, "unit_benefit")

    assert not benefit.signal.present
    assert "undefined" in benefit.signal.note
    # Dropped and renormalised (D.0), never scored 0.
    assert component.coverage < 1.0
    assert component.points > 0


def test_the_undefined_tables_carry_the_question_the_workshop_must_answer(config):
    """An empty table is only honest if it says what it is empty *of*."""
    resources = config.rule("unit_benefit")["resources"]
    for resource_type in ResourceType:
        section = resources[resource_type.value]
        assert section["question"].strip(), resource_type.value
        if not section["entries"]:
            assert section["status"] == "undefined_needs_clinical_definition"


def test_the_ward_question_records_the_inversion(config):
    """Not a rescoring of "does ICU help" — the opposite question."""
    ward = config.rule("unit_benefit")["resources"]["ward_bed"]["question"].lower()
    assert "sufficient" in ward


# -- Step 10 · ICU can bid, because it finally has an Operational formula ------------------


def _with_icu_bidder():
    """Ward-Patient-C rebid by ICU: same patient and data, different department.

    Holding the patient constant is the point — it isolates the eligibility and formula
    change from any difference in the candidate.
    """
    ward_c = next(c for c in fx.CANDIDATES if c.candidate_id == "Ward-Patient-C")
    icu_c = replace(ward_c, candidate_id="ICU-Patient-D", agent=AgentKind.ICU)
    patients = dict(fx.PATIENT_DATA)
    patients["ICU-Patient-D"] = replace(patients["Ward-Patient-C"], candidate=icu_c)
    return fx.FixtureDataSource(patients=patients), (*fx.CANDIDATES, icu_c)


def _run_with_icu(config, resource_type: ResourceType):
    source, candidates = _with_icu_bidder()
    return run_allocation(
        config, source, candidates, fx.NOW, profile=REGISTRY.get(resource_type)
    )


@pytest.mark.parametrize("resource_type", [ResourceType.WARD_BED, ResourceType.HDU_BED])
def test_icu_bids_for_a_step_down_bed(config, resource_type):
    """The department with the strongest claim on a step-down bed can finally make it."""
    assert AgentKind.ICU in REGISTRY.get(resource_type).eligible_agents
    run = _run_with_icu(config, resource_type)
    assert "ICU-Patient-D" in run.utilities


def test_icu_is_scored_at_full_operational_coverage(config):
    """F-D: an eligible-but-unscored bidder would run at reduced coverage against the rest.

    Before Step 10, Operational returned Signal.absent for ICU. Making it eligible without a
    formula would have handicapped it permanently rather than let it compete.
    """
    run = _run_with_icu(config, ResourceType.WARD_BED)
    operational = _component(run, "ICU-Patient-D", ComponentName.OPERATIONAL)
    assert operational.coverage == 1.0
    assert operational.points > 0


def test_the_icu_formula_declares_what_it_does_not_measure(config):
    """It is nursing load with the ward's unfitted saturation constant, not ICU bed pressure."""
    run = _run_with_icu(config, ResourceType.WARD_BED)
    note = _component(run, "ICU-Patient-D", ComponentName.OPERATIONAL).factors[0].signal.note
    assert "unfitted" in note
    assert "not the bed pressure" in note


@pytest.mark.parametrize(
    "resource_type",
    [ResourceType.ICU_BED, ResourceType.PACU_BED, ResourceType.RESUS_BED, ResourceType.ED_BED],
)
def test_icu_does_not_bid_where_it_has_no_claim(config, resource_type):
    """Patients do not step down from ICU into these units.

    ``icu_bed`` is the pointed one: whether ICU bids for an ICU bed as *internal demand* is a
    separate open question (F-12 / AGENT_BUDGET decision 3) and Step 10 does not settle it.
    """
    assert AgentKind.ICU not in REGISTRY.get(resource_type).eligible_agents
    run = _run_with_icu(config, resource_type)
    assert "ICU-Patient-D" not in run.utilities


def test_appendix_c_still_has_exactly_three_bidders(config):
    """Adding a fourth AgentKind must not change the auction the worked example pins."""
    run = _run(config, ResourceType.ICU_BED)
    assert set(run.utilities) == {"ER-Patient-A", "OT-Patient-B", "Ward-Patient-C"}


# -- Steps 7 + 11 · one budget pool, and one scarcity value, per resource type -------------


def test_every_bed_has_its_own_caps_and_budget_tables(config):
    """No two resources may share either — one is the calibration, the other the pool."""
    caps = {r: REGISTRY.get(r).caps_config for r in ResourceType}
    pools = {r: REGISTRY.get(r).budget_config for r in ResourceType}
    assert len(set(caps.values())) == len(ResourceType)
    assert len(set(pools.values())) == len(ResourceType)
    for resource_type in ResourceType:
        assert caps[resource_type] in config.caps_files
        assert pools[resource_type] in config.budget_files


def test_selecting_a_resource_selects_its_pool(config):
    """D-3: the pool moves with the resource, not just the caps."""
    for resource_type in (ResourceType.ICU_BED, ResourceType.WARD_BED):
        scoped = config.for_resource(REGISTRY.get(resource_type))
        assert scoped.budget["resource_type"] == resource_type.value
        assert scoped.caps["resource_type"] == resource_type.value


def test_scarcity_is_no_longer_scoped_global(config):
    """D-4: 'an identical multiplier on every budget' was never true across bed types.

    ICU at 100% and wards at 60% are two numbers about two different units.
    """
    for resource_type in ResourceType:
        scoped = config.for_resource(REGISTRY.get(resource_type))
        assert scoped.budget["factors"]["scarcity"]["scope"] == "per_resource_type"


def test_a_budget_from_another_resource_is_refused(config):
    """The D-3 failure mode, made unreachable rather than merely discouraged.

    A budget row stamps the caps_version its points were denominated in. Carrying an ICU pool
    into a ward auction is what would let ICU auctions, at ~107 points each, drain what
    ward-bed auctions bid ~50 into.
    """
    icu_budgets = _run(config, ResourceType.ICU_BED).opening_budgets

    with pytest.raises(ValueError, match="own pool"):
        run_allocation(
            config, fx.FixtureDataSource(), fx.CANDIDATES, fx.NOW,
            profile=REGISTRY.get(ResourceType.WARD_BED),
            budgets=icu_budgets,
        )


def test_the_same_resources_own_budgets_are_accepted(config):
    """The guard must not block the thing carrying budgets is for — a sequence of auctions."""
    first = _run(config, ResourceType.ICU_BED)
    second = run_allocation(
        config, fx.FixtureDataSource(), fx.CANDIDATES, fx.NOW,
        profile=REGISTRY.get(ResourceType.ICU_BED),
        budgets=first.opening_budgets,
    )
    assert second.winner is not None


def test_an_unfitted_pool_announces_itself(config):
    """Separate but uncalibrated. A pool that is too large makes bidding maximum free."""
    icu = config.for_resource(REGISTRY.get(ResourceType.ICU_BED)).unsigned
    ward = config.for_resource(REGISTRY.get(ResourceType.WARD_BED)).unsigned

    assert icu["budget.pool.icu_bed"] == "assumed_pending_burn_rate_data"
    assert ward["budget.pool.ward_bed"] == "unfitted_copied_from_icu_bed"
    # And only the selected pool is reported — not all six.
    assert [k for k in ward if k.startswith("budget.pool.")] == ["budget.pool.ward_bed"]


def test_an_in_place_config_override_survives_resource_selection(config):
    """``replace(config, budget=...)`` must not be silently discarded by for_resource.

    Otherwise a caller adjusting a table in memory would quietly be testing the shipped file.
    """
    from dataclasses import replace as dc_replace

    tweaked = dc_replace(
        config,
        budget={**config.budget, "base": {**config.budget["base"], "common_points": 2100}},
    )
    scoped = tweaked.for_resource(REGISTRY.get(ResourceType.ICU_BED))
    assert scoped.budget["base"]["common_points"] == 2100


# -- Step 12 · the auction reads the beds it is auctioning ---------------------------------


def test_the_fixture_describes_every_unit_that_can_be_auctioned(config):
    """A seventh bed type must come with a seventh fixture state.

    Without this, adding one would leave every fixture-based auction of it raising at the
    ingest seam — the right failure, found at the wrong time. Asserted as set equality so a
    stray state for a unit no resource sits in is caught too.
    """
    assert set(fx.UNIT_STATES) == {resource_type.unit for resource_type in ResourceType}


@pytest.mark.parametrize("resource_type", list(ResourceType))
def test_each_auction_reads_its_own_units_beds(config, resource_type):
    """The behavioural gap Stages 1-5 left open: every auction read the ICU fixture.

    ``hospital_state`` took no unit, so an HDU auction priced itself off ICU's 20/20. The
    six fixture occupancies are deliberately all different, so reading the wrong unit shows
    up as a wrong number rather than a plausible one.
    """
    run = _run(config, resource_type)
    expected = fx.UNIT_STATES[resource_type.unit]

    assert run.snapshot.hospital.unit == resource_type.unit
    assert run.snapshot.hospital.occupancy == pytest.approx(expected.occupancy)


def test_occupancy_is_no_longer_the_same_number_in_every_auction(config):
    """The observable consequence, stated as a fact about the output rather than the wiring.

    Six auctions of the same three patients used to report one occupancy — ICU's. Six
    distinct values is what closing the gap looks like from outside.
    """
    read = {
        _run(config, resource_type).snapshot.hospital.occupancy
        for resource_type in ResourceType
    }
    assert len(read) == len(ResourceType)


def _ward_run(config, occupied: int):
    """A ward-bed auction differing from the fixture in nothing but the ward's occupancy."""
    source = fx.FixtureDataSource(
        hospital=replace(fx.UNIT_STATES["ward"], unit_occupied_beds=occupied)
    )
    return run_allocation(
        config, source, fx.CANDIDATES, fx.NOW,
        profile=REGISTRY.get(ResourceType.WARD_BED),
    )


def test_occupancy_reaches_the_price_not_just_the_log(config):
    """The unit-scoped read has to be spent, not merely recorded.

    Occupancy drives the reserve price, contention and scarcity. If the ward's own occupancy
    changed none of them, reading the right unit would be decorative.
    """
    quiet, full = _ward_run(config, 24), _ward_run(config, 40)

    assert full.outcome.result.reserve_price > quiet.outcome.result.reserve_price
    assert full.outcome.result.contention > quiet.outcome.result.contention
    assert (
        next(iter(full.opening_budgets.values())).scarcity
        > next(iter(quiet.opening_budgets.values())).scarcity
    )


def test_the_occupancy_onset_has_no_gradient_left_for_a_small_unit(config):
    """C-9, and the reason the test above compares 60% with 100% rather than 60% with 80%.

    All three occupancy mechanisms — reserve, scarcity, contention — begin at a *fraction*,
    0.85, chosen when ICU's 20 beds were the only beds auctioned. On 20 beds that leaves a
    usable gradient (18/20 -> 0.33, 19/20 -> 0.67). On resus's 6 it leaves none: the whole
    0.85-1.00 band falls between the 5th bed and the 6th, so a resus down to its last bed
    scores as unstressed and one bed later scores maximum.

    Before Step 12 this was unreachable — every auction read ICU's 20/20, so every occupancy
    mechanism sat at its ceiling in every auction. Fitting an onset per unit needs the same
    governance workshop as the caps and the pools; until then this pins the arithmetic, so
    nobody adjusts 0.85 believing it is already a per-unit figure.
    """
    resus = fx.UNIT_STATES["resus"]
    assert (resus.unit_total_beds, resus.unit_occupied_beds) == (6, 5)

    def run(occupied: int):
        source = fx.FixtureDataSource(hospital=replace(resus, unit_occupied_beds=occupied))
        return run_allocation(
            config, source, fx.CANDIDATES, fx.NOW,
            profile=REGISTRY.get(ResourceType.RESUS_BED),
        )

    runs = {n: run(n) for n in (3, 4, 5, 6)}
    scarcity = {n: next(iter(r.opening_budgets.values())).scarcity for n, r in runs.items()}
    reserve = {n: r.outcome.result.reserve_price for n, r in runs.items()}

    # Half full, two-thirds full and one bed left are the same number to both budget-side
    # mechanisms. Only the sixth bed registers at all.
    assert scarcity[3] == scarcity[4] == scarcity[5] == 1.0
    assert scarcity[6] > 1.0
    assert len({round(runs[n].outcome.result.contention, 6) for n in (3, 4, 5)}) == 1

    # And within that band the reserve moves the *wrong way*: filling the unit lowers the
    # minimum acceptable bid, because the only occupancy term still live below the onset is
    # Resource Stress, which lowers the ceiling the reserve is a fraction of.
    assert reserve[5] < reserve[4] < reserve[3]
    assert reserve[6] > reserve[3]


def test_a_unit_the_source_cannot_describe_is_refused(config):
    """An ICU-only world cannot answer a ward-bed auction, and must not pretend to.

    This is the same rule as "no default profile", one layer down: substituting ICU's beds
    yields a reserve price, a contention factor and six budget figures that are all
    internally consistent and all about a different unit.
    """
    icu_only = fx.FixtureDataSource(hospital=fx.HOSPITAL_STATE)

    with pytest.raises(ValueError, match="no fixture hospital state for unit 'ward'"):
        run_allocation(
            config, icu_only, fx.CANDIDATES, fx.NOW,
            profile=REGISTRY.get(ResourceType.WARD_BED),
        )


def test_a_source_that_answers_with_the_wrong_unit_is_caught(config):
    """A data source may raise, but it may not answer a different question than it was asked.

    Nothing downstream reads ``HospitalState.unit``, so an adapter that ignored the argument
    would be undetectable at every later layer. The check belongs at the seam every
    implementation crosses — which is where the Hasura reader will cross it too.
    """
    class AlwaysIcu:
        async def hospital_state(self, unit, at):
            return fx.HOSPITAL_STATE          # unit='icu', whatever was asked for

        async def patient_data(self, candidate, at):
            return fx.PATIENT_DATA[candidate.candidate_id]

    with pytest.raises(ValueError, match="returned state for unit 'icu'"):
        run_allocation(
            config, AlwaysIcu(), fx.CANDIDATES, fx.NOW,
            profile=REGISTRY.get(ResourceType.WARD_BED),
        )


def test_the_ingest_trace_names_the_unit_it_read(config):
    """The row that would have exposed the bug all along, now that it says something true."""
    run = _run(config, ResourceType.WARD_BED)
    rows = dict(run.trace["ingest"].rows)

    assert rows["WARD occupancy"] == "24/40 = 60%"
