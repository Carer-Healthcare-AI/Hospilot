"""Refusing to write a row that cannot be used later.

This module is the point of the audit layer. Everything downstream — the ICU-benefit model,
the criticality factor, fairness, and cap fitting — depends on records that **nothing
backfills**. A log that silently drops the losers, or stores a utility total without its
components, or omits the caps version, is not a partially useful log. It is a log that looks
fine for a year and then cannot answer the question it was collected for.

So a bundle that fails any check below raises rather than writing. The cost of a failed write
is an alert; the cost of a bad write is discovered eighteen months later.
"""

from __future__ import annotations

from allocation.audit.records import AuditBundle


class IncompleteAuditRecord(ValueError):
    """A bundle that would be unusable for the models that depend on it."""


def violations(bundle: AuditBundle) -> tuple[str, ...]:
    """Every reason this bundle should not be written, in the order they matter."""
    found: list[str] = []
    auction = bundle.auction

    # --- versioning ---------------------------------------------------------------
    # Budgets are denominated in utility points, so a cap change re-derives everything.
    # A row with no version cannot be re-derived and is invisible to B.13.
    if not auction.caps_version or not auction.config_version:
        found.append("auction is missing caps_version or config_version")

    # --- the losers ---------------------------------------------------------------
    if not bundle.bids:
        found.append("no bid rows: an auction with no bids records nothing")

    # Checked against the declared participant list, not against the agents that happen to
    # appear in the bid rows. Comparing the rows to themselves proves only that they are
    # self-consistent — a bundle with every loser stripped out would pass.
    participants = set(auction.participants)
    if not participants:
        found.append(
            "auction records no participants; a candidate that was eligible but never bid "
            "is a denial, and B.10 is built from denials"
        )
    else:
        first_round = {bid.agent for bid in bundle.bids if bid.round_index == 0}
        dropped = sorted(participants - first_round)
        if dropped:
            found.append(
                f"round 0 does not record every participant (missing {dropped}); losers' "
                "opening bids are what B.10 and B.12 are built from"
            )

    # --- the component breakdown ---------------------------------------------------
    for bid in bundle.bids:
        if not bid.component_points:
            found.append(
                f"{bid.agent} round {bid.round_index}: no component breakdown — "
                "B.13 cap fitting needs per-component values on contested cases"
            )
            break
    for bid in bundle.bids:
        if not bid.component_coverage:
            found.append(
                f"{bid.agent} round {bid.round_index}: no coverage fractions — "
                "a score computed from half its inputs is not the same score"
            )
            break

    # --- provenance ----------------------------------------------------------------
    for bid in bundle.bids:
        if not bid.policy_name:
            found.append(
                f"{bid.agent} round {bid.round_index}: no policy_name — behaviour data and "
                "evaluation data must stay separable in the log"
            )
            break

    # --- reproducibility ------------------------------------------------------------
    if not bundle.snapshots:
        found.append(
            "no utility_snapshot rows: a score that cannot be re-derived is useless to "
            "cap fitting"
        )
    else:
        rounds_with_bids = {bid.round_index for bid in bundle.bids}
        rounds_with_snapshots = {snap.round_index for snap in bundle.snapshots}
        missing = sorted(rounds_with_bids - rounds_with_snapshots)
        if missing:
            found.append(f"rounds {missing} have bids but no snapshot")

    # --- internal consistency --------------------------------------------------------
    if auction.rounds_run != len({bid.round_index for bid in bundle.bids}):
        found.append("rounds_run disagrees with the distinct round indices in the bid rows")

    if auction.outcome == "awarded":
        if auction.winning_agent is None or auction.winning_bid is None:
            found.append("outcome is 'awarded' but no winner is recorded")
        elif auction.winning_bid < auction.reserve_price - 1e-9:
            found.append("winning bid is below the reserve price")

    for bid in bundle.bids:
        if bid.action != "withdraw" and bid.amount > bid.ceiling + 1e-9:
            found.append(f"{bid.agent} round {bid.round_index}: bid exceeds its own ceiling")
            break

    return tuple(found)


def ensure_writable(bundle: AuditBundle) -> AuditBundle:
    """Raise unless the bundle is complete enough to be worth keeping."""
    problems = violations(bundle)
    if problems:
        raise IncompleteAuditRecord(
            f"auction {bundle.auction_id} would write an unusable record:\n  - "
            + "\n  - ".join(problems)
        )
    return bundle
