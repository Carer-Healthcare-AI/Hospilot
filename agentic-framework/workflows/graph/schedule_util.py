"""Cadence helpers for scheduled queries (autonomous mode, Phase 6).

Validate a cron expression and compute the next fire time for either an interval
or a cron schedule. Kept dependency-light (datetime + apscheduler's CronTrigger used
purely as a next-fire calculator -- no running scheduler) so both the request models
(schemas/models.py) and the scheduler loop (workflows/graph/scheduler.py) can import
it without pulling in the runner/hasura import graph.
"""

from datetime import datetime, timedelta, timezone as _tz

from apscheduler.triggers.cron import CronTrigger

UNIT_SECONDS = {"minutes": 60, "hours": 3600, "days": 86400}


def interval_from(every: int, unit: str) -> int:
    """Convert a friendly (every, unit) pair to seconds."""
    if unit not in UNIT_SECONDS:
        raise ValueError(f"unit must be one of {list(UNIT_SECONDS)}")
    if every <= 0:
        raise ValueError("every must be a positive integer")
    return every * UNIT_SECONDS[unit]


def validate_cron(expr: str, tz: str = "UTC") -> None:
    """Raise ValueError if expr is not a valid 5-field crontab (or tz is unknown)."""
    try:
        CronTrigger.from_crontab(expr, timezone=tz)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"invalid cron expression or timezone: {e}") from e


def next_fire_time(
    *, schedule_kind: str, interval_seconds: int | None, cron_expr: str | None,
    tz: str, from_dt: datetime,
) -> datetime:
    """The next fire strictly after `from_dt`, returned tz-aware in UTC.

    interval -> from_dt + interval_seconds.
    cron     -> the next crontab match after from_dt (computed in `tz`).
    """
    if from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=_tz.utc)
    if schedule_kind == "cron":
        trigger = CronTrigger.from_crontab(cron_expr, timezone=tz)
        # +1s so a from_dt sitting exactly on a boundary advances to the NEXT match
        # (get_next_fire_time is inclusive of `now`), never refiring the same instant.
        nxt = trigger.get_next_fire_time(None, from_dt + timedelta(seconds=1))
        if nxt is None:
            raise ValueError("cron expression yields no future fire time")
        return nxt.astimezone(_tz.utc)
    if not interval_seconds or interval_seconds <= 0:
        raise ValueError("interval_seconds must be a positive integer")
    return from_dt + timedelta(seconds=interval_seconds)
