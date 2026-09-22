"""Detect imaging contention and open a diagnostic auction.

The counterpart to ``rl_gateway/trigger.py``'s bed-release detection. The trigger condition is
different: a bed auction opens when a bed is *released*, but an imaging machine is contended
whenever two or more eligible orders are pending against the same modality — the machine does
not have to become free for the queue to need ordering.

**Why a lock per (org, modality).** Two orders arriving a second apart would otherwise open two
auctions for the same scanner, and both would believe the machine was free. The bed trigger
locks per (org, resource) for the same reason.

**Not wired by default.** Like the bed trigger, nothing calls `on_imaging_order` until the app
opts in. See §7 of DIAGNOSTIC_MACHINE_BACKEND_INTEGRATION.md for the rollout phases.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from rl_gateway_diagnostic.assemble import MissingClinicalValue, build_auction_body
from rl_gateway_diagnostic.client import DiagnosticClient, DiagnosticEngineError
from rl_gateway_diagnostic.persist import persist_auction
from rl_gateway_diagnostic.queries import NODE_TO_DEPARTMENT, modality_for

log = logging.getLogger("rl_gateway_diagnostic.trigger")

#: Below this many pending eligible orders there is no contention, so there is nothing to
#: arbitrate and the order goes straight to scheduling. Opening an auction for one request
#: would still produce a ladder, but it would be theatre.
MIN_CONTENTION = 2

_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


def _lock(org: str, modality: str) -> asyncio.Lock:
    return _locks[(org, modality)]


def department_of(order: Mapping[str, Any]) -> str | None:
    """Which bidding department holds this patient."""
    node = order.get("agent_node") or order.get("owning_agent")
    if node:
        return NODE_TO_DEPARTMENT().get(str(node))
    unit = str(order.get("unit") or order.get("ward") or "").lower()
    if unit in ("er", "ed", "emergency"):
        return "er"
    if unit in ("ot", "theatre", "theater"):
        return "ot"
    if unit == "icu":
        return "icu"
    return None


def group_by_modality(
    orders: Sequence[Mapping[str, Any]]
) -> dict[str, list[tuple[Mapping[str, Any], str]]]:
    """Pending orders -> {modality: [(order, department)]}, dropping what cannot bid."""
    grouped: dict[str, list[tuple[Mapping[str, Any], str]]] = defaultdict(list)
    for order in orders:
        procedure = str(order.get("procedure") or order.get("description") or "")
        modality = modality_for(procedure)
        department = department_of(order)
        if modality is None or department is None:
            log.debug("skipping order %s: modality=%s department=%s",
                      order.get("id"), modality, department)
            continue
        grouped[modality].append((order, department))
    return dict(grouped)


def nominate(
    candidates: Sequence[tuple[Mapping[str, Any], str]]
) -> list[tuple[Mapping[str, Any], str]]:
    """One order per department — the earliest ordered.

    The engine allocates one interval, so a department with three pending CTs must choose
    which patient it is bidding for. Earliest-ordered is a defensible default and a *stated*
    one; a hospital that wants sickest-first should change it here, visibly.
    """
    best: dict[str, tuple[Mapping[str, Any], str]] = {}
    for order, department in candidates:
        at = str(order.get("ordered_at") or order.get("created_at") or "")
        current = best.get(department)
        if current is None or at < str(
            current[0].get("ordered_at") or current[0].get("created_at") or ""
        ):
            best[department] = (order, department)
    return list(best.values())


async def open_auction(
    execute,
    orders: Sequence[Mapping[str, Any]],
    modality: str,
    org_slug: str = "default",
    now: datetime | None = None,
    regime: str = "normal",
) -> dict[str, Any] | None:
    """Assemble, run and persist one diagnostic auction. Returns the response, or None."""
    now = now or datetime.now(timezone.utc)
    async with _lock(org_slug, modality):
        nominated = nominate([(o, department_of(o) or "") for o in orders])
        if len(nominated) < MIN_CONTENTION:
            log.debug("no contention on %s (%d eligible)", modality, len(nominated))
            return None
        try:
            body = build_auction_body(modality, nominated, now, regime=regime)
        except MissingClinicalValue as exc:
            # A refusal is the correct outcome, not an error to paper over: the alternative
            # is ranking a real patient against a value nobody supplied.
            log.warning("diagnostic auction not opened for %s: %s", modality, exc)
            return None
        try:
            response = await DiagnosticClient().run_auction(body)
        except DiagnosticEngineError as exc:
            log.warning("engine refused the %s auction (%s): %s",
                        modality, exc.status, exc.body)
            return None
        await persist_auction(execute, response, trigger_source="imaging_order")
        return response


async def on_imaging_order(
    execute,
    pending_orders: Sequence[Mapping[str, Any]],
    org_slug: str = "default",
) -> list[dict[str, Any]]:
    """Entry point: called with every pending imaging order after one is created.

    Opens at most one auction per modality per call.
    """
    results = []
    for modality, candidates in group_by_modality(pending_orders).items():
        response = await open_auction(
            execute, [o for o, _ in candidates], modality, org_slug=org_slug
        )
        if response is not None:
            results.append(response)
    return results
