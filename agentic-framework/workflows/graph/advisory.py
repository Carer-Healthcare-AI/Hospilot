"""Advisory engine -- event-first evaluation of notify-only rules.

Rules (hospilot_app.advisory_rules) are evaluated two ways, per row:

  event path  -- the Kafka data consumer (messaging/data_consumer.py) calls
                 notify_entity_change(entity) after every hospilot.data.* Redis
                 update; enabled rules whose trigger_entities include a changed
                 entity are evaluated within one wake, debounced per rule so an
                 event burst collapses to a single evaluation.
  clock path  -- rules with check_interval_seconds set are scanned like the
                 query scheduler (workflows/graph/scheduler.py): every tick,
                 rows with next_check_at <= now are evaluated. This carries the
                 conditions no event can (SLA timeouts, time-rolling forecasts,
                 cooldown re-alerts in quiet periods, boot catch-up).

Both paths share check_rule(): resolve the python evaluator by rule_key
(workflows/graph/advisory_evaluators.py), run it, gate on cooldown, insert an
advisories row on fire, and always advance bookkeeping -- a failing or unknown
evaluator can never hot-loop or kill the engine. One asyncio task, launched in
main.py's lifespan; durable state lives in the rule row (next_check_at,
last_fired_at), so restarts are safe. Single-API-replica assumption, same as
the scheduler/reaper.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from config import settings
from db.hasura import hasura
from workflows.graph.advisory_conditions import run_condition
from workflows.graph.advisory_evaluators import EVALUATORS
from workflows.graph.exec_context import set_exec_ctx

logger = logging.getLogger(__name__)

# Event-nudge state: the data consumer adds changed entity names and sets the
# wake event; the engine task drains the set. In-memory on purpose -- a lost
# nudge only costs latency (the clock path is the safety net), never data.
_dirty: set[str] = set()
_wake = asyncio.Event()

# (org_id, rule_id) -> monotonic time of last event-driven evaluation.
_last_event_eval: dict[tuple[str | None, str], float] = {}


def notify_entity_change(entity: str) -> None:
    """Nudge from the Kafka data consumer: `entity` just changed. Sync + never
    raises, so the consumer stays decoupled; a no-op where the engine isn't
    running (e.g. the Temporal worker never starts it)."""
    try:
        _dirty.add(entity)
        _wake.set()
    except Exception:  # noqa: BLE001
        pass


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def check_rule(rule: dict, org_id: str | None, now: datetime, source: str) -> bool:
    """Evaluate one rule and record the outcome. Returns True if an advisory was
    inserted. Bookkeeping (last_checked_at, and next_check_at for clock rules) is
    advanced even when the evaluator raises, so bad rules retry on cadence instead
    of hot-looping."""
    rule_id = rule["id"]
    rule_key = rule["rule_key"]

    set_fields: dict = {"last_checked_at": now.isoformat()}
    interval = rule.get("check_interval_seconds")
    if interval:
        interval = max(int(interval), settings.advisory_min_check_interval_seconds)
        set_fields["next_check_at"] = (now + timedelta(seconds=interval)).isoformat()

    fired_row = False
    try:
        # Rule logic lives in the DB `definition` JSON (declarative condition or a
        # named handler). Fall back to a code evaluator by rule_key for rules that
        # have no definition yet (DB-first / pre-migration). A rule with neither is
        # skipped quietly until its logic lands.
        condition = (rule.get("definition") or {}).get("condition")
        if not condition and rule_key not in EVALUATORS:
            logger.warning("advisory rule %s has no condition or evaluator (skipped)", rule_key)
        else:
            # Route Fabric reads (fget X-Org-Id) made while evaluating.
            set_exec_ctx(session_id=f"advisory:{rule_id}", agent_id="advisory_engine",
                         org_id=org_id or "")
            if condition:
                fired, detail, data = await run_condition(condition, org_id)
            else:
                # No definition yet (brand-new rule): thresholds would come from the
                # definition, so nothing to pass -- safety-net path only.
                fired, detail, data = await EVALUATORS[rule_key](org_id, {})

            last_fired = _parse_ts(rule.get("last_fired_at"))
            cooldown = int(rule.get("cooldown_seconds") or 0)
            in_cooldown = bool(last_fired and last_fired + timedelta(seconds=cooldown) > now)

            if fired and not in_cooldown:
                await hasura.insert_advisory(
                    rule_key=rule_key, topic=rule["topic"], severity=rule["severity"],
                    title=rule["label"], detail=detail, data=data or {},
                    suggested_action=rule.get("suggested_action"), org_id=org_id,
                )
                set_fields["last_fired_at"] = now.isoformat()
                set_fields["fire_count"] = (rule.get("fire_count") or 0) + 1
                fired_row = True
                logger.info("advisory fired  rule=%s  source=%s  org=%s  detail=%s",
                            rule_key, source, org_id, detail)
            elif fired:
                logger.info("advisory suppressed (cooldown)  rule=%s  org=%s", rule_key, org_id)
    except Exception:  # noqa: BLE001
        logger.exception("advisory evaluation failed  rule=%s  org=%s", rule_key, org_id)

    try:
        await hasura.update_advisory_rule(rule_id, set_fields, org_id=org_id)
    except Exception:  # noqa: BLE001
        logger.exception("advisory bookkeeping failed  rule=%s  org=%s", rule_key, org_id)
    return fired_row


async def _org_ids() -> list[str | None]:
    try:
        await hasura.ensure_org_registry()
        return [o["id"] for o in hasura.active_orgs()] or [None]
    except Exception:  # noqa: BLE001
        return [None]


async def _run_event_rules(entities: set[str], now: datetime) -> int:
    """Evaluate enabled rules whose trigger_entities intersect the changed set,
    per org, skipping rules evaluated within the debounce window."""
    debounce = settings.advisory_event_debounce_seconds
    fired = 0
    for org_id in await _org_ids():
        try:
            rules = await hasura.fetch_event_advisory_rules(org_id=org_id)
        except Exception:  # noqa: BLE001
            logger.exception("advisory event scan failed  org=%s", org_id)
            continue
        for rule in rules:
            triggers = set(rule.get("trigger_entities") or [])
            if not triggers & entities:
                continue
            key = (org_id, rule["id"])
            if time.monotonic() - _last_event_eval.get(key, 0.0) < debounce:
                continue
            _last_event_eval[key] = time.monotonic()
            if await check_rule(rule, org_id, now, source="event"):
                fired += 1
    return fired


async def _run_due_clock_rules(now: datetime) -> int:
    """Clock tick: evaluate rules with an interval whose next_check_at has passed."""
    fired = 0
    for org_id in await _org_ids():
        try:
            due = await hasura.fetch_due_advisory_rules(now.isoformat(), org_id=org_id)
        except Exception:  # noqa: BLE001
            logger.exception("advisory clock scan failed  org=%s", org_id)
            continue
        for rule in due:
            if await check_rule(rule, org_id, now, source="clock"):
                fired += 1
    return fired


async def start_advisory_engine() -> None:
    """Run the engine forever (launched as a background task at startup). Sleeps on
    the wake event with the scan interval as timeout: an event nudge wakes it
    immediately, otherwise it ticks for the clock path."""
    interval = settings.advisory_scan_interval_seconds
    logger.info("[ok] advisory engine started  scan_interval=%ds  event_debounce=%ds  kafka=%s",
                interval, settings.advisory_event_debounce_seconds,
                "on" if settings.kafka_enabled else "OFF (event-only rules will not evaluate)")
    last_clock_scan = 0.0
    while True:
        try:
            await asyncio.wait_for(_wake.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

        try:
            now = datetime.now(timezone.utc)
            if _wake.is_set():
                _wake.clear()
                entities = set(_dirty)
                _dirty.clear()
                if entities:
                    await _run_event_rules(entities, now)
            # Run the clock scan on cadence even under a continuous event stream
            # (event wakes must not starve interval rules).
            if time.monotonic() - last_clock_scan >= interval:
                last_clock_scan = time.monotonic()
                await _run_due_clock_rules(now)
        except Exception:  # noqa: BLE001
            logger.exception("advisory engine iteration failed")
