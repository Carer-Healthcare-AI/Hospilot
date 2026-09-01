"""Configuration: caps, thresholds, budget parameters, auction mechanics, rule tables.

Everything that could differ between resource types or be revised without a code change
lives here. The split between :mod:`caps` and :mod:`thresholds` is deliberate — thresholds
are cheap to revise, caps re-derive every budget in the system.
"""

from allocation.config.loader import CONFIG_DIR, RULES_DIR, Config, load_config

__all__ = ["CONFIG_DIR", "RULES_DIR", "Config", "load_config"]
