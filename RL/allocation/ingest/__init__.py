"""Raw rows in. No scoring, no judgement, no magic numbers.

If a constant appears in this layer, the layering has broken: normalisation belongs to
``features/`` and weighting to ``utility/``.

Everything reaches the engine through the :class:`~allocation.contracts.DataSource` protocol.
:class:`~allocation.ingest.fixtures.FixtureDataSource` serves Appendix C so the whole utility
stack runs with no database; a Hasura implementation replaces it later without touching any
layer above this one.
"""

from allocation.ingest.fixtures import FixtureDataSource
from allocation.ingest.snapshot import build_snapshot, build_snapshot_sync

__all__ = ["FixtureDataSource", "build_snapshot", "build_snapshot_sync"]
