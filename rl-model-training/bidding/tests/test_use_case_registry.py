"""Public discovery and entry-point boundaries for resource families."""

from __future__ import annotations

import importlib

import pytest

from allocation.use_cases import USE_CASES, get_use_case


def test_supported_use_cases_are_registered_without_eager_domain_imports():
    assert tuple(sorted(USE_CASES)) == (
        "bed",
        "diagnostic_machine",
    )
    assert get_use_case("bed").module == "allocation.use_cases.bed"
    assert get_use_case("diagnostic_machine").module == (
        "allocation.use_cases.diagnostic_machine"
    )


@pytest.mark.parametrize("name", sorted(USE_CASES))
def test_registered_use_case_package_exists(name):
    module = importlib.import_module(USE_CASES[name].module)
    assert module is not None


def test_unknown_use_case_names_the_valid_choices():
    with pytest.raises(
        KeyError, match="bed, diagnostic_machine"
    ):
        get_use_case("theatre")
