"""Weights, caps, coverage renormalisation, and the eight components.

Entry point::

    from allocation.config import load_config
    from allocation.profiles import ICU_BED
    from allocation.utility import build_engine

    engine = build_engine(load_config(), ICU_BED)
    breakdown = engine.score(candidate, snapshot)
"""

from allocation.config import Config
from allocation.profiles.registry import ResourceProfile
from allocation.utility.ceiling import Ceiling, ceiling_for
from allocation.utility.components import build_components
from allocation.utility.engine import UtilityEngine, weighted

__all__ = ["Ceiling", "UtilityEngine", "build_engine", "build_components", "ceiling_for", "weighted"]


def build_engine(config: Config, profile: ResourceProfile) -> UtilityEngine:
    """Assemble the engine for one resource profile."""
    return UtilityEngine(config, profile, build_components(config, profile))
