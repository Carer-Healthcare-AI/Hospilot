"""Assembling one immutable :class:`FeatureSnapshot` per auction round.

**One read of the world per round, shared by every component.** Without this, two components
can call ``/icu/occupancy`` a second apart, disagree, and produce a utility that cannot be
reproduced from the log — which makes B.13 cap fitting impossible and makes any RL episode
built on it unreliable.

The snapshot is also the unit that gets persisted to ``allocation.utility_snapshot``, so a
score can be re-derived months later against a different set of caps.
"""

from __future__ import annotations

import asyncio

from allocation.ingest.loop import run as _run
import hashlib
from datetime import datetime
from typing import Sequence

from allocation.config import Config
from allocation.contracts import Candidate, DataSource, FeatureSnapshot


def _snapshot_id(auction_id: str, round_index: int, taken_at: datetime) -> str:
    raw = f"{auction_id}:{round_index}:{taken_at.isoformat()}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


async def build_snapshot(
    source: DataSource,
    candidates: Sequence[Candidate],
    taken_at: datetime,
    config: Config,
    unit: str,
    auction_id: str = "adhoc",
    round_index: int = 0,
) -> FeatureSnapshot:
    """Read ``unit``'s hospital state and every candidate's data at one instant.

    Patient reads are issued concurrently — they are independent — but they all carry the
    same ``taken_at``, so a slow query cannot make one bidder's data newer than another's.

    ``unit`` is the unit of the bed being auctioned (``ResourceType.unit``). It is required
    and has no default: every consumer of ``hospital.occupancy`` — reserve price, contention,
    scarcity, the budget factors — is asking about the beds under auction, so the wrong unit
    here is not a smaller version of the right answer.
    """
    hospital = await source.hospital_state(unit, taken_at)
    if hospital.unit != unit:
        # A DataSource that answers with a different unit than it was asked for is worse than
        # one that raises: nothing downstream reads the label, so the substitution would be
        # invisible. Checked here rather than trusted, because this is the seam every
        # implementation crosses — fixture, scenario, and the Hasura reader alike.
        raise ValueError(
            f"{type(source).__name__}.hospital_state({unit!r}) returned state for unit "
            f"{hospital.unit!r}. A data source must serve the unit it was asked for or "
            "raise; substituting another unit's beds silently reprices the auction."
        )
    patients = await asyncio.gather(
        *(source.patient_data(candidate, taken_at) for candidate in candidates)
    )

    return FeatureSnapshot(
        snapshot_id=_snapshot_id(auction_id, round_index, taken_at),
        taken_at=taken_at,
        hospital=hospital,
        patients={c.candidate_id: d for c, d in zip(candidates, patients, strict=True)},
        caps_version=config.caps_version,
        config_version=config.config_version,
    )


def build_snapshot_sync(
    source: DataSource,
    candidates: Sequence[Candidate],
    taken_at: datetime,
    config: Config,
    unit: str,
    auction_id: str = "adhoc",
    round_index: int = 0,
) -> FeatureSnapshot:
    """Blocking convenience wrapper for tests and CLI use. Not for use inside the workflow."""
    return _run(
        build_snapshot(source, candidates, taken_at, config, unit, auction_id, round_index)
    )
