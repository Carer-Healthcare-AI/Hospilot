"""Bed-allocation use case.

The bed family — ``icu > hdu > pacu > resus > ed > ward``, the care ladder as
``config/rules/units.yaml`` orders it. Its resource profiles live in ``profiles/``; importing
that package registers all six with the shared profile registry.

**Configuration is deliberately not here.** The caps and budget tables stay in
``allocation/config/`` because ``config_version`` is a digest over every file the loader globs
from one base directory, and because ``use_cases.diagnostic_machine.config.for_modality``
merges its modality tables into a base :class:`~allocation.config.Config` loaded from that same
directory. Splitting the config seam per family is a loader change, not a file move.

Run it with ``python -m allocation.use_cases.bed``. Existing ``python -m allocation`` callers
remain supported.
"""

NAME = "bed"

__all__ = ["NAME"]
