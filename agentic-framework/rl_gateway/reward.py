"""The 4-hour reward loop: enqueue on close, poll when due, observe, score, persist outcome.

Durable by design — the engine's PendingObservation is meant to survive a restart but ships
as an in-memory dataclass (A3); allocation.pending_observation (migration 126) is that store.

ObservationSource reads what actually happened in the window after close. Every term reader
returns True | False | None where None means NOT KNOWN — never a default. An unwired reader
returning None is CORRECT, not a stub-cheat: it excludes that term and marks the episode
incomplete, which is exactly the designed behaviour (and no_mortality/F-01 keeps every episode
incomplete regardless). Fill readers in over time; the pipeline runs and persists throughout.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from rl_gateway.scoring import HORIZON_HOURS, score

log = logging.getLogger("rl_gateway.reward")


# --- pending_observation store ---------------------------------------------------------------

_ENQUEUE_SQL = """
INSERT INTO allocation.pending_observation (auction_id, closed_at, horizon_hours, due_at)
VALUES (%s, %s, %s, %s)
ON CONFLICT (auction_id) DO NOTHING
"""

_POLL_DUE_SQL = """
SELECT auction_id, closed_at, horizon_hours
FROM allocation.pending_observation
WHERE status = 'pending' AND due_at <= now()
ORDER BY due_at
LIMIT %s
"""

_MARK_OBSERVED_SQL = """
UPDATE allocation.pending_observation
SET status = 'observed', observed_at = now()
WHERE auction_id = %s
"""

_MARK_ATTEMPT_SQL = """
UPDATE allocation.pending_observation
SET attempts = attempts + 1, last_attempt_at = now(), last_error = %s,
    status = CASE WHEN attempts + 1 >= 5 THEN 'abandoned' ELSE status END
WHERE auction_id = %s
"""

_OUTCOME_SQL = """
INSERT INTO allocation.auction_outcome (
    auction_id, horizon_hours, terms, reward_total,
    mortality_observed, mortality_source, complete, missing_terms
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (auction_id) DO NOTHING
"""


async def enqueue_observation(execute: Callable, resp: Mapping[str, Any]) -> None:
    """Queue an auction's reward window from its response (resp['reward'].due_at/horizon)."""
    reward = resp.get("reward") or {}
    await execute(_ENQUEUE_SQL, (
        resp["auction_id"],
        resp.get("closed_at"),
        reward.get("horizon_hours", HORIZON_HOURS),
        reward.get("due_at"),
    ))


# --- observation -----------------------------------------------------------------------------

class ObservationSource:
    """Reads the post-close window from Hasura. One method per reward term; each returns
    True | False | None. `auction` is the persisted allocation.auction row (winner, unit, etc.)
    giving each reader its subject.
    """

    def __init__(self, hasura: Any) -> None:
        self._h = hasura

    async def observe(self, auction: Mapping[str, Any]) -> dict[str, bool | None]:
        # Only terms that could apply to this auction are included; the rest are simply not
        # passed (scoring treats absence-from-the-map as "did not apply", not "missing").
        obs: dict[str, bool | None] = {}
        won = auction.get("outcome") == "awarded"
        # Won-scenario terms apply when a bed was awarded; lost-scenario when it was not.
        # Readers are wired incrementally — None until then (honest "unknown").
        if won:
            obs["transferred_to_icu"] = await self._transferred_to_icu(auction)
            obs["patient_stabilised"] = await self._patient_stabilised(auction)
            obs["boarding_reduced"] = await self._boarding_reduced(auction)
        else:
            obs["patient_deterioration"] = await self._patient_deterioration(auction)
            obs["additional_boarding"] = await self._additional_boarding(auction)
        return obs

    # F-01: no structured mortality source — handled separately in run_due_observations and
    # always unknown. Kept explicit so it can never be silently defaulted.

    async def _transferred_to_icu(self, a) -> bool | None:
        # Did the winning patient actually reach an ICU bed in the window? (source: ipd_admissions)
        # Our nomination sets candidate_id = patient_token, so winning_candidate_id is the token.
        token = a.get("winning_candidate_id")
        if not token:
            return None
        try:
            icu = await self._h.get_icu_admissions() or []
        except Exception as exc:  # noqa: BLE001
            log.warning("observe transferred_to_icu: %s", exc)
            return None
        return token in {adm.get("patient_token") for adm in icu}

    async def _patient_stabilised(self, a): return await self._todo("patient_stabilised")
    async def _boarding_reduced(self, a): return await self._todo("boarding_reduced")
    async def _patient_deterioration(self, a): return await self._todo("patient_deterioration")
    async def _additional_boarding(self, a): return await self._todo("additional_boarding")

    async def _todo(self, term: str) -> None:
        # Reader not yet wired to its source table -> unknown. Correct and safe: the term is
        # excluded and the episode stays incomplete until this is implemented.
        return None


# --- the batch job ---------------------------------------------------------------------------

_LOAD_AUCTION_SQL = """
SELECT id AS auction_id, outcome, winning_agent, winning_candidate_id, resource_type,
       resource_id, closed_at
FROM allocation.auction WHERE id = %s
"""


async def run_due_observations(execute: Callable, hasura: Any, limit: int = 50) -> int:
    """Poll due pending rows, observe+score each, persist the outcome. Returns count scored.
    Call on a schedule (e.g. every few minutes) inside a tenant_transaction."""
    import json

    observer = ObservationSource(hasura)
    due = await _fetch(execute, _POLL_DUE_SQL, (limit,))
    scored = 0
    for row in due:
        auction_id = row["auction_id"]
        try:
            auction = (await _fetch(execute, _LOAD_AUCTION_SQL, (auction_id,)))[0]
            observations = await observer.observe(auction)
            result = score(observations, mortality_observed=None)  # F-01: always unknown
            await execute(_OUTCOME_SQL, (
                auction_id, HORIZON_HOURS, json.dumps(result["terms"]),
                result["reward_total"], result["mortality_observed"], None,
                result["complete"], result["missing_terms"],
            ))
            await execute(_MARK_OBSERVED_SQL, (auction_id,))
            scored += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("observation failed for auction %s: %s", auction_id, exc)
            await execute(_MARK_ATTEMPT_SQL, (str(exc)[:500], auction_id))
    if scored:
        log.info("reward loop scored %d auction(s)", scored)
    return scored


async def _fetch(execute: Callable, sql: str, params: tuple) -> list[dict]:
    """execute() is fire-and-forget; for SELECTs we need rows. The tenant_transaction's
    execute closes over an AsyncConnection — use its cursor via the same connection. Callers
    pass an execute bound to a connection that also exposes .fetch (see db.tenant_cursor)."""
    return await execute(sql, params, fetch=True)  # type: ignore[call-arg]
