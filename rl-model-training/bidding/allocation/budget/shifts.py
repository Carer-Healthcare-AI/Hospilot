"""Shift boundaries and temporal planning for the budget layer.

The shift map is configured in ``config/rules/shifts.yaml`` and resolved to concrete time
windows for each run. An unmatched label raises rather than defaulting, so incorrect shift
boundaries fail loudly rather than silently corrupting budgeting and burn-rate metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping

from allocation.config import Config

try:  # pragma: no cover - platform dependent
    from zoneinfo import ZoneInfo

    _HAVE_ZONEINFO = True
except ImportError:  # pragma: no cover
    _HAVE_ZONEINFO = False


@dataclass(frozen=True, slots=True)
class Shift:
    """One rostered shift, resolved to absolute times."""

    label: str
    shift_id: str
    start: datetime
    end: datetime

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def elapsed_hours(self, moment: datetime) -> float:
        """Hours into the shift, clamped to the shift's own length."""
        raw = (moment - self.start).total_seconds() / 3600.0
        return max(0.0, min(raw, self.hours))


def shift_timezone(config: Config) -> Any:
    """The roster's timezone.

    Shift boundaries are wall-clock times on the ward, so every comparison happens in local
    time. A UTC-shaped assumption puts an Indian night shift in the wrong day. Falls back to
    UTC when ``tzdata`` is unavailable — which is itself worth knowing about, because the
    fallback silently shifts every boundary.
    """
    name = str(config.rule("shifts").get("timezone", "UTC"))
    if name.upper() == "UTC" or not _HAVE_ZONEINFO:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - missing tzdata on some Windows installs
        return timezone.utc


def _parse(value: str) -> time:
    hour, minute = (int(part) for part in str(value).split(":"))
    return time(hour=hour, minute=minute)


def _candidates(config: Config, day: date, tz: Any) -> list[Shift]:
    out: list[Shift] = []
    for row in config.rule("shifts")["shifts"]:
        label = str(row["label"])
        start_t, end_t = _parse(row["start"]), _parse(row["end"])
        start = datetime.combine(day, start_t, tzinfo=tz)
        end = datetime.combine(day, end_t, tzinfo=tz)
        if end <= start:  # the night shift crosses midnight
            end += timedelta(days=1)
        out.append(Shift(label=label, shift_id=f"{day.isoformat()}:{label}", start=start, end=end))
    return out


def resolve_shift(config: Config, moment: datetime) -> Shift:
    """The shift containing ``moment``.

    Yesterday's shifts are considered too, so a night shift that began before midnight is
    matched rather than falling through.
    """
    tz = shift_timezone(config)
    local = moment.astimezone(tz)
    for day in (local.date() - timedelta(days=1), local.date()):
        for shift in _candidates(config, day, tz):
            if shift.contains(local):
                return shift
    raise ValueError(
        f"no shift in config/rules/shifts.yaml contains {local.isoformat()}; "
        "the shifts must tile the day without gaps"
    )


def next_shift(config: Config, shift: Shift) -> Shift:
    """The shift beginning where this one ends."""
    return resolve_shift(config, shift.end)


def normalise_label(config: Config, roster_value: str) -> str:
    """Map a ``staff_roster.shift`` value onto a configured label.

    Raises on an unknown value rather than guessing — see the module note.
    """
    aliases: Mapping[str, Any] = config.rule("shifts")["label_aliases"]
    needle = roster_value.strip().lower()
    for label, values in aliases.items():
        if needle == label or needle in {str(v).strip().lower() for v in values}:
            return label
    raise ValueError(
        f"staff_roster.shift value {roster_value!r} is not in label_aliases "
        f"(known: {sorted(aliases)}). Add it rather than letting it default — F-17."
    )
