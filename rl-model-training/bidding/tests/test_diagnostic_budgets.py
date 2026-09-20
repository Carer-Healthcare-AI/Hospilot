"""Diagnostic budget pools, the ICU bidder declaration, and the simulation regimes.

The regime tests re-measure the burn distribution rather than asserting the numbers in the
config file are plausible. A calibration that has silently gone inert is the failure mode
worth catching: AGENT_BUDGET section 8 is explicit that below a 0.40 burn rate the constraint
stops existing and "bidding maximum is free, and the RL will learn to do exactly that". A
simulator that only ever runs inert budgets produces training data from a world where
spending costs nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.use_cases.diagnostic_machine import (
    MODALITIES,
    DiagnosticModality,
    diagnostic_shift,
    for_modality,
    open_diagnostic_budgets,
    simulate,
)
from allocation.use_cases.diagnostic_machine.budgets import REGIMES, simulation_base
from allocation.use_cases.diagnostic_machine.scenarios import EPOCH, SCENARIOS

CT = DiagnosticModality.CT


@pytest.fixture(scope="module")
def config():
    return load_config()


def burn_across_scenarios(config, regime: str | None) -> list[float]:
    """Every agent's burn rate in every scenario, under one regime."""
    out: list[float] = []
    for build in SCENARIOS.values():
        scenario = build()
        scoped = for_modality(config, scenario.modality)
        shift = diagnostic_shift(EPOCH, hours=8.0)
        budgets = open_diagnostic_budgets(
            scoped, scenario.profile, shift, machines=scenario.machines, regime=regime
        )
        result = simulate(
            scoped,
            scenario.profile,
            scenario.machines,
            scenario.arrivals,
            budgets,
            starts_at=shift.start,
            ends_at=shift.start + timedelta(hours=scenario.hours),
            alternative_machines=scenario.alternative_machines,
        )
        out.extend(result.metrics.burn_rate.values())
    return sorted(out)


def median(values: list[float]) -> float:
    return values[len(values) // 2]


# -- the finding this fixes ------------------------------------------------------------------


def test_the_live_pool_is_inert_in_simulation(config):
    """The finding, pinned. At the shipped Base a scenario burns ~2 % and nothing binds."""
    burns = burn_across_scenarios(config, regime=None)
    inert_below = float(for_modality(config, CT).budget["burn_rate_bands"]["inert_below"])
    assert max(burns) < inert_below, (
        "the live pool has become non-inert in simulation; the regimes below were calibrated "
        "against it being inert and should be re-measured"
    )


@pytest.mark.parametrize("regime", REGIMES)
def test_every_regime_leaves_the_inert_band(config, regime):
    """Each regime must actually make the budget do something."""
    burns = burn_across_scenarios(config, regime)
    inert_below = float(for_modality(config, CT).budget["burn_rate_bands"]["inert_below"])
    assert median(burns) > inert_below


def test_the_regimes_are_ordered(config):
    """normal -> constrained -> exhausted must be a real progression, not three labels."""
    normal = burn_across_scenarios(config, "normal")
    constrained = burn_across_scenarios(config, "constrained")
    assert median(constrained) > median(normal)


def test_normal_still_serves_what_capacity_allows(config):
    """A budget that binds must not be a budget that decides the allocation."""
    scenario = SCENARIOS["three_way_contention"]()
    scoped = for_modality(config, scenario.modality)
    shift = diagnostic_shift(EPOCH, hours=8.0)
    result = simulate(
        scoped,
        scenario.profile,
        scenario.machines,
        scenario.arrivals,
        open_diagnostic_budgets(
            scoped, scenario.profile, shift, machines=scenario.machines, regime="normal"
        ),
        starts_at=shift.start,
        ends_at=shift.start + timedelta(hours=scenario.hours),
    )
    assert result.metrics.answered == result.metrics.requests


def test_exhausted_starves_requests_capacity_could_have_served(config):
    """The regime that proves an unaffordable exit is reachable at all.

    The scanner is free the whole time. Nothing is answered, and the reason is the budget —
    which is exactly the state the live pool can never demonstrate.
    """
    scenario = SCENARIOS["three_way_contention"]()
    scoped = for_modality(config, scenario.modality)
    shift = diagnostic_shift(EPOCH, hours=8.0)
    result = simulate(
        scoped,
        scenario.profile,
        scenario.machines,
        scenario.arrivals,
        open_diagnostic_budgets(
            scoped, scenario.profile, shift, machines=scenario.machines, regime="exhausted"
        ),
        starts_at=shift.start,
        ends_at=shift.start + timedelta(hours=scenario.hours),
    )
    assert result.metrics.answered < result.metrics.requests
    assert result.metrics.utilisation < 0.1  # capacity was there; budget was not


def test_an_unknown_regime_is_refused_not_defaulted(config):
    """Falling back to the live Base would produce an inert run wearing a regime label."""
    scoped = for_modality(config, CT)
    with pytest.raises(KeyError, match="unknown simulation regime"):
        simulation_base(scoped, "comfortable")


def test_regimes_are_declared_simulation_only(config):
    for modality in DiagnosticModality:
        simulation = for_modality(config, modality).budget["simulation"]
        assert simulation["status"] == "simulation_only_not_a_live_allowance"


def test_a_regime_scales_the_field_together(config):
    """A regime must not reorder who can afford what — it scales Base, never the factors."""
    scoped = for_modality(config, CT)
    profile = MODALITIES.get(CT)
    shift = diagnostic_shift(EPOCH)
    live = open_diagnostic_budgets(scoped, profile, shift)
    scaled = open_diagnostic_budgets(scoped, profile, shift, regime="normal")
    ratios = {a: scaled[a].budget_total / live[a].budget_total for a in live}
    assert len(set(round(r, 9) for r in ratios.values())) == 1
    for agent in live:
        assert scaled[agent].demand == live[agent].demand
        assert scaled[agent].criticality == live[agent].criticality


# -- ICU is an intentional bidder, with its parameters marked unfitted ------------------------


def test_icu_is_declared_an_intentional_bidder(config):
    """Not an accident of inheriting the bed agent list."""
    for modality in DiagnosticModality:
        icu = for_modality(config, modality).budget["bidders"]["icu"]
        assert icu["eligible"] is True
        assert icu["status"] == "intentional_bidder_parameters_unfitted"


def test_icu_unfitted_parameters_are_enumerated(config):
    """A declaration with no list of what is unfitted is a declaration nobody can act on."""
    for modality in DiagnosticModality:
        icu = for_modality(config, modality).budget["bidders"]["icu"]
        assert len(icu["unfitted"]) >= 3
        assert icu["open_decision"]


def test_the_bidder_declaration_matches_the_profile(config):
    """Config and code must not disagree about who may bid."""
    for modality in DiagnosticModality:
        declared = {
            name
            for name, row in for_modality(config, modality).budget["bidders"].items()
            if row["eligible"]
        }
        profile = MODALITIES.get(modality)
        assert declared == {a.value for a in profile.eligible_agents}


def test_icu_criticality_is_still_ers_number(config):
    """Pinned so that fitting it is a visible change rather than a quiet one."""
    factors = for_modality(config, CT).budget["factors"]["criticality"]["values"]
    assert factors["icu"] == factors["er"], (
        "ICU criticality now differs from ER's — if that was fitted, update the unfitted list "
        "in the pool file and this test"
    )


def test_icu_holds_a_budget_and_can_win(config):
    """The bed use case never had to make this work. Here it must."""
    scenario = SCENARIOS["three_way_contention"]()
    scoped = for_modality(config, scenario.modality)
    shift = diagnostic_shift(EPOCH, hours=8.0)
    budgets = open_diagnostic_budgets(
        scoped, scenario.profile, shift, machines=scenario.machines
    )
    assert AgentKind.ICU in budgets
    assert budgets[AgentKind.ICU].budget_total > 0
    result = simulate(
        scoped, scenario.profile, scenario.machines, scenario.arrivals, budgets,
        starts_at=shift.start, ends_at=shift.start + timedelta(hours=scenario.hours),
    )
    assert result.metrics.wins["icu"] >= 1
