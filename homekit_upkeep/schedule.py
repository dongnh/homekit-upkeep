"""Due-time math. All datetimes are naive local time, the same convention as
light-programmer's schedule engine."""
from datetime import datetime, time, timedelta
from typing import Optional

DEFAULT_NOTIFY = "09:00"


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def parse_last_done(iso) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:  # store writes naive local; normalise strays
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def battery_pct(last_done: datetime, due: datetime, now: datetime) -> int:
    """Time left before `due` as a 0-100 "battery" of the current cycle:
    100 right after mark-done, draining linearly to 0 at the due moment."""
    span = (due - last_done).total_seconds()
    if span <= 0:
        return 0
    remaining = (due - now).total_seconds()
    return max(0, min(100, round(100.0 * remaining / span)))


def due_at(last_done: datetime, interval_days: float, notify: time) -> datetime:
    """When the task next becomes due: `interval_days` after `last_done`,
    snapped to that day's `notify` time so reminders fire at a humane hour —
    day-granularity chores shouldn't inherit the time-of-day you happened to
    finish at. For sub-day intervals the snap could land before `last_done`;
    then the exact interval end is used instead (handy for testing)."""
    base = last_done + timedelta(days=interval_days)
    snapped = datetime.combine(base.date(), notify)
    return snapped if snapped > last_done else base
