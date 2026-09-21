"""Modality profiles and per-modality configuration.

The claim under test is the one the whole family rests on: four modalities, one algorithm,
and adding a fifth touches a profile and two config files and nothing else.
"""

from __future__ import annotations

import pytest

from allocation.config import load_config
from allocation.contracts import AgentKind, ResourceType
from allocation.use_cases.bed.profiles import REGISTRY
from allocation.use_cases.diagnostic_machine import (
    INITIAL_ASSUMED_CAPS,
    MODALITIES,
    DiagnosticComponent,
    DiagnosticModality,
    for_modality,
)
from allocation.use_cases.diagnostic_machine.config import with_diagnostic_tables
from allocation.use_cases.diagnostic_machine.scoring import caps_from_config


@pytest.fixture(scope="module")
def config():
    return load_config()


def test_every_modality_is_registered():
    registered = {profile.modality for profile in MODALITIES.all()}
    assert registered == set(DiagnosticModality)


def test_icu_bids_in_every_modality():
    """The difference from every bed profile, and an unsettled governance question (D-A)."""
    for profile in MODALITIES.all():
        assert AgentKind.ICU in profile.eligible_agents


def test_every_modality_has_its_own_caps_and_pool(config):
    """No two modalities may share either — one is the calibration, the other the pool."""
    scoped = with_diagnostic_tables(config)
    caps = {p.modality: p.caps_config for p in MODALITIES.all()}
    pools = {p.modality: p.budget_config for p in MODALITIES.all()}
    assert len(set(caps.values())) == len(DiagnosticModality)
    assert len(set(pools.values())) == len(DiagnosticModality)
    for modality in DiagnosticModality:
        assert caps[modality] in scoped.caps_files
        assert pools[modality] in scoped.budget_files


def test_selecting_a_modality_selects_its_caps_and_pool(config):
    for modality in DiagnosticModality:
        scoped = for_modality(config, modality)
        assert scoped.caps["resource_type"] == modality.value
        assert scoped.budget["resource_type"] == modality.value


def test_each_modality_stamps_a_distinct_caps_version(config):
    """Two modalities' audit rows must not claim the same calibration."""
    versions = {for_modality(config, m).caps_version for m in DiagnosticModality}
    assert len(versions) == len(DiagnosticModality)


def test_caps_files_match_the_component_scale_in_code(config):
    """The yaml and ``INITIAL_ASSUMED_CAPS`` are one scale, and cannot drift apart."""
    for modality in DiagnosticModality:
        scoped = for_modality(config, modality)
        assert caps_from_config(scoped.caps) == dict(INITIAL_ASSUMED_CAPS)


def test_caps_declare_every_component(config):
    for modality in DiagnosticModality:
        table = for_modality(config, modality).caps["components"]
        assert set(table) == {c.value for c in DiagnosticComponent}


def test_caps_and_pools_are_declared_unsigned(config):
    """An assumption nobody can see is one nobody will revisit."""
    for modality in DiagnosticModality:
        unsigned = for_modality(config, modality).unsigned
        assert f"caps.{modality.value}" in unsigned
        assert f"budget.pool.{modality.value}" in unsigned


def test_occupies_includes_setup_and_cleanup():
    """A scanner booked at its nominal duration is over-booked."""
    profile = MODALITIES.get(DiagnosticModality.CT)
    held = profile.occupies(profile.typical_duration)
    assert held > profile.typical_duration
    assert held.total_seconds() / 60 == (
        profile.typical_duration.total_seconds() / 60
        + profile.setup_minutes
        + profile.cleanup_minutes
    )


def test_mri_is_the_scarce_one():
    """The property that makes MRI worth auctioning: it holds the machine far longer."""
    mri = MODALITIES.get(DiagnosticModality.MRI)
    ct = MODALITIES.get(DiagnosticModality.CT)
    assert mri.occupies(mri.typical_duration) > 2 * ct.occupies(ct.typical_duration)


def test_portable_modalities_are_marked():
    assert MODALITIES.get(DiagnosticModality.X_RAY).portable
    assert MODALITIES.get(DiagnosticModality.ULTRASOUND).portable
    assert not MODALITIES.get(DiagnosticModality.MRI).portable


def test_ttl_lookup_names_the_modality_when_missing():
    profile = MODALITIES.get(DiagnosticModality.CT)
    with pytest.raises(KeyError, match="ct"):
        profile.ttl_for(AgentKind.WARD)


def test_registering_a_modality_twice_fails():
    from allocation.use_cases.diagnostic_machine.profiles import ModalityRegistry

    registry = ModalityRegistry()
    profile = MODALITIES.get(DiagnosticModality.CT)
    registry.register(profile)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(profile)


# -- the bed family must not have noticed any of this --------------------------------------


def test_diagnostic_profiles_are_not_in_the_bed_registry():
    """Query resolution and ``GET /use-cases`` enumerate the bed registry. It has six rows."""
    assert len(REGISTRY.all()) == len(ResourceType)
    assert {p.resource_type for p in REGISTRY.all()} == set(ResourceType)


def test_diagnostic_tables_do_not_move_the_bed_config(config):
    """Adding diagnostic tables must not restamp a bed auction's versions."""
    scoped = with_diagnostic_tables(config)
    assert scoped.config_version == config.config_version
    assert scoped.caps_version == config.caps_version
    assert scoped.caps["resource_type"] == config.caps["resource_type"]


def test_bed_profiles_still_select_bed_tables(config):
    """A config that has seen the diagnostic tables still scopes a bed correctly."""
    scoped = with_diagnostic_tables(config).for_resource(REGISTRY.get(ResourceType.ICU_BED))
    assert scoped.caps["resource_type"] == "icu_bed"
    assert scoped.budget["resource_type"] == "icu_bed"
