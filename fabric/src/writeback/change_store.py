"""Pending-changes store — a single-in-flight snapshot state machine.

Writes from the main app are queued here as PendingChanges. The DB drains them in
a two-phase, soft-locked exchange:

  1. GET  /fhir/Bundle/$pending-changes        — Fabric mints a snapshot_id, moves all
     queued changes into the (single) in-flight snapshot (state "offered"), and returns
     them as a FHIR Bundle. Re-pulling returns the SAME snapshot until it's resolved.
  2. POST /fhir/Bundle/$pending-changes/$acknowledge {snapshot_id}
     — receipt. Marks the snapshot "locked" (the soft lock is now held).
  3. POST /fhir/Bundle/$pending-changes/$confirm {snapshot_id, results[]}
     — the DB reports accepted/rejected per change. Fabric publishes one ack event per
     change to Kafka and clears the snapshot (releases the lock).

Only ONE snapshot is in flight at a time (the DB applies one batch before the next is
offered). Writes that arrive while a snapshot is in flight queue up for the next pull.
If the DB never confirms within `timeout_s`, the lock EXPIRES and the changes re-enter
the queue (at-least-once) — see `current_inflight`.

The GET handler resolves deferred ids (critical_vital, ai_discharge_note) OUTSIDE the
store lock, then commits the resolved set via `commit_inflight`, so the in-flight
snapshot and the Bundle reference the exact same change_ids.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class PendingChange:
    change_type: str       # "bed_status" | "triage_score" | "discharge_ready" | ...
    resource_type: str     # FHIR type: "Encounter", "Location", "Observation", etc.
    resource_id: str | None  # None when the ID must be resolved at bundle-build time
    http_method: str       # "PATCH" | "POST"
    payload: dict          # all data needed to build the Bundle entry
    timestamp: str         # ISO 8601 UTC
    change_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # stable per-change id
    entity: str = ""       # logical Kafka entity for the ack event ("bed", "visit", ...)
    record_id: str = ""    # bare id the backend used (drives the ack event's `id`)
    approval_needed: bool = False  # DB must route through approval before applying


@dataclass
class Snapshot:
    snapshot_id: str
    changes: list[PendingChange]
    state: str             # "offered" (pulled, not yet acked) | "locked" (acked, awaiting confirm)
    created_at: float      # time.monotonic() at offer
    locked_at: float | None = None


class SnapshotError(Exception):
    """Raised when an $acknowledge/$confirm references an unknown/mismatched snapshot."""


class ChangeStore:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._pending: list[PendingChange] = []
        self._inflight: Snapshot | None = None

    async def add(self, change: PendingChange) -> None:
        async with self._lock:
            self._pending.append(change)

    async def peek_pending(self) -> list[PendingChange]:
        """Return a snapshot of the queued changes WITHOUT removing them (kafka write
        mode). The publisher resolves + publishes each, then calls `remove` for the ones
        that delivered — so a crash mid-publish leaves changes queued (at-least-once)
        rather than lost. Preserves FIFO order. Ignores the in-flight snapshot, which is
        never used in kafka write mode (the HTTP pull is disabled there)."""
        async with self._lock:
            return list(self._pending)

    async def remove(self, change_ids: set[str]) -> None:
        """Drop the given changes from the queue by change_id (kafka write mode: called
        after a proposal has been successfully published)."""
        if not change_ids:
            return
        async with self._lock:
            self._pending = [c for c in self._pending if c.change_id not in change_ids]

    async def current_inflight(self, timeout_s: float) -> Snapshot | None:
        """Return the live in-flight snapshot for an idempotent re-pull, or None if there
        is none. If the in-flight snapshot has exceeded `timeout_s` without confirmation,
        its changes are returned to the FRONT of the queue and None is returned (so the
        caller offers a fresh snapshot)."""
        async with self._lock:
            if self._inflight is None:
                return None
            if time.monotonic() - self._inflight.created_at < timeout_s:
                return self._inflight
            self._pending = self._inflight.changes + self._pending   # expired: requeue
            self._inflight = None
            return None

    async def drain_pending(self) -> list[PendingChange]:
        """Atomically remove and return all queued (not-yet-offered) changes. Only valid
        when no snapshot is in flight (the caller checks `current_inflight` first)."""
        async with self._lock:
            if self._inflight is not None:
                return []
            drained = self._pending
            self._pending = []
            return drained

    async def commit_inflight(self, resolved: list[PendingChange]) -> Snapshot | None:
        """Create the in-flight snapshot from the resolved change set. If resolution
        dropped everything, returns None. If a snapshot raced in ahead of us, the resolved
        changes are returned to the queue and the existing snapshot wins."""
        async with self._lock:
            if not resolved:
                return None
            if self._inflight is not None:
                self._pending = resolved + self._pending
                return self._inflight
            snap = Snapshot(
                snapshot_id=uuid.uuid4().hex,
                changes=list(resolved),
                state="offered",
                created_at=time.monotonic(),
            )
            self._inflight = snap
            return snap

    async def mark_locked(self, snapshot_id: str) -> Snapshot:
        """Receipt ($acknowledge): the DB has durably received the snapshot."""
        async with self._lock:
            if self._inflight is None or self._inflight.snapshot_id != snapshot_id:
                raise SnapshotError(f"no in-flight snapshot {snapshot_id!r}")
            self._inflight.state = "locked"
            self._inflight.locked_at = time.monotonic()
            return self._inflight

    async def inflight_changes(self, snapshot_id: str) -> list[PendingChange]:
        """Return the in-flight snapshot's changes for $confirm, WITHOUT releasing the
        lock. The caller publishes acks first, then calls `release` only on success — so
        a publish failure leaves the same snapshot (same id) locked for the DB to retry."""
        async with self._lock:
            if self._inflight is None or self._inflight.snapshot_id != snapshot_id:
                raise SnapshotError(f"no in-flight snapshot {snapshot_id!r}")
            return list(self._inflight.changes)

    async def release(self, snapshot_id: str) -> None:
        """Clear the in-flight snapshot (lock released) once its acks are published."""
        async with self._lock:
            if self._inflight is not None and self._inflight.snapshot_id == snapshot_id:
                self._inflight = None


_store = ChangeStore()


def get_change_store() -> ChangeStore:
    return _store


def new_change_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
