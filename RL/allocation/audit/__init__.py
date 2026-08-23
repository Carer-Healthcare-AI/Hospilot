"""The log. Everything blocked in the framework is blocked on this, and none of it backfills.

    B.10  Expected ICU benefit   needs patients who were DENIED a bed
    B.11  Criticality            needs request timestamps
    B.12  Fairness v2/v3         needs win/loss history weighted by utility forgone
    B.13  Cap fitting            needs contested cases with per-component values
    BA2   Mean aggression alpha  needs ~50 observed auctions
    -     Burn rate              needs one shift

Four design choices follow from that, and each is enforced rather than documented:

**Every agent, every round, including withdrawals.** A log of winners answers no question
worth asking. :mod:`allocation.audit.validate` refuses a bundle that drops them.

**The component breakdown, not the total.** Cap fitting needs to know that ER's 107 was 45 of
Clinical Benefit and 24 of Urgency; the total tells it nothing.

**The versions on every row.** Budgets are denominated in utility points, so a cap change
re-derives everything. A row with no ``caps_version`` cannot be re-derived and is invisible to
the exercise it was collected for.

**One transaction.** The budget decrement and the bid rows land together or not at all.
"""

from allocation.audit.records import (
    AuctionRow,
    AuditBundle,
    BidRow,
    BudgetRow,
    OutcomeRow,
    SnapshotRow,
)
from allocation.audit.sink import (
    AuditSink,
    DuplicateAuction,
    InMemorySink,
    JsonlSink,
    PostgresSink,
)
from allocation.audit.sql import statements
from allocation.audit.validate import IncompleteAuditRecord, ensure_writable, violations
from allocation.audit.writer import build_bundle, build_outcome_row

__all__ = [
    "AuctionRow",
    "AuditBundle",
    "AuditSink",
    "BidRow",
    "BudgetRow",
    "DuplicateAuction",
    "IncompleteAuditRecord",
    "InMemorySink",
    "JsonlSink",
    "OutcomeRow",
    "PostgresSink",
    "SnapshotRow",
    "build_bundle",
    "build_outcome_row",
    "ensure_writable",
    "statements",
    "violations",
]
