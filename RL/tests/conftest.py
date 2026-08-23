"""Shared fixtures. Everything is built from Appendix C — no database, no network."""

from __future__ import annotations

import pytest

from allocation.config import Config, load_config
from allocation.contracts import FeatureSnapshot
from allocation.ingest import FixtureDataSource, build_snapshot_sync
from allocation.ingest.fixtures import NOW
from allocation.profiles import ICU_BED
from allocation.profiles.registry import ResourceProfile
from allocation.utility import UtilityEngine, build_engine


@pytest.fixture(scope="session")
def config() -> Config:
    return load_config()


@pytest.fixture(scope="session")
def config_dir():
    """The shipped config directory, for tests that copy and edit it."""
    from allocation.config.loader import CONFIG_DIR

    return CONFIG_DIR


@pytest.fixture(scope="session")
def profile() -> ResourceProfile:
    return ICU_BED


@pytest.fixture(scope="session")
def engine(config: Config, profile: ResourceProfile) -> UtilityEngine:
    return build_engine(config, profile)


@pytest.fixture(scope="session")
def snapshot(config: Config) -> FeatureSnapshot:
    """Appendix C's ICU state — the unit ``ICU_BED`` auctions, and the one C.1 describes."""
    from allocation.ingest.fixtures import CANDIDATES

    return build_snapshot_sync(
        FixtureDataSource(), CANDIDATES, NOW, config, ICU_BED.resource_type.unit,
        "test-auction", 0,
    )
