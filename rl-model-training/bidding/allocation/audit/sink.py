"""Where the rows go.

Three implementations, one protocol. The Postgres one is a thin wrapper over
:mod:`allocation.audit.sql`, which produces parameterised statements without importing any
driver — so this package still has no dependency on the framework it will eventually run in.

**Idempotency lives here, not in the caller.** A re-firing discharge prediction can produce
the same ``auction_key``, and section 7 requires that it must not open two auctions on one
bed. Live auctions are unique on that key; simulation, advisory and replay runs are
deliberately exempt so testing never collides with a real allocation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, Sequence

from allocation.audit.records import AuditBundle, OutcomeRow
from allocation.audit.serialise import jsonable


class DuplicateAuction(ValueError):
    """A live auction already exists for this ``auction_key``."""


class AuditSink(Protocol):
    def write(self, bundle: AuditBundle) -> None: ...

    def record_outcome(self, row: OutcomeRow) -> None: ...


class InMemorySink:
    """For tests and the simulator. Enforces the same idempotency rule as Postgres."""

    def __init__(self) -> None:
        self.bundles: list[AuditBundle] = []
        self.outcomes: list[OutcomeRow] = []
        self._live_keys: set[str] = set()

    def write(self, bundle: AuditBundle) -> None:
        key = bundle.auction.auction_key
        if bundle.auction.mode == "live":
            if key in self._live_keys:
                raise DuplicateAuction(
                    f"a live auction already exists for {key!r}; a re-firing discharge "
                    "prediction must not open a second auction on one bed"
                )
            self._live_keys.add(key)
        self.bundles.append(bundle)

    def record_outcome(self, row: OutcomeRow) -> None:
        self.outcomes.append(row)

    # -- reading back, for tests and off-line analysis ------------------------------

    def bids(self) -> list[dict[str, Any]]:
        return [jsonable(bid) for bundle in self.bundles for bid in bundle.bids]

    def training_bundles(self) -> list[AuditBundle]:
        """Only live auctions. A simulation never held a bed or moved a budget."""
        return [b for b in self.bundles if b.auction.mode == "live"]


class JsonlSink:
    """Append-only JSONL, one object per row, typed by a ``_table`` key.

    This is what lets the simulator generate training data with no database at all, and what
    makes an auction log portable between environments.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, bundle: AuditBundle) -> None:
        rows: list[tuple[str, Any]] = [("auction", bundle.auction)]
        rows += [("auction_bid", bid) for bid in bundle.bids]
        rows += [("agent_budget", budget) for budget in bundle.budgets]
        rows += [("utility_snapshot", snap) for snap in bundle.snapshots]
        self._append(rows)

    def record_outcome(self, row: OutcomeRow) -> None:
        self._append([("auction_outcome", row)])

    def _append(self, rows: Sequence[tuple[str, Any]]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            for table, row in rows:
                fh.write(json.dumps({"_table": table, **jsonable(row)}) + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class PostgresSink:
    """Executes the statements from :mod:`allocation.audit.sql` inside one transaction.

    ``executor`` is any callable taking ``(sql, params)`` — a psycopg cursor, an asyncpg
    connection wrapper, or the framework's Hasura client. The driver is the caller's business;
    this package never imports one.

    The whole bundle goes in a single transaction because the budget decrement and the bid
    rows must land together or not at all.
    """

    def __init__(self, executor: Any, begin: str = "BEGIN", commit: str = "COMMIT",
                 rollback: str = "ROLLBACK") -> None:
        self._execute = executor
        self._begin, self._commit, self._rollback = begin, commit, rollback

    def write(self, bundle: AuditBundle) -> None:
        from allocation.audit.sql import statements

        self._execute(self._begin, ())
        try:
            for sql, params in statements(bundle):
                self._execute(sql, params)
        except Exception:
            self._execute(self._rollback, ())
            raise
        self._execute(self._commit, ())

    def record_outcome(self, row: OutcomeRow) -> None:
        from allocation.audit.sql import outcome_statement

        sql, params = outcome_statement(row)
        self._execute(sql, params)
