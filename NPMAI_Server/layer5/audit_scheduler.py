"""
Our own scheduling logic - no APScheduler, no cron library. Default
audit times are 09:00, 13:45, 22:18 (matching this project's own shared
architecture spec), but every building can override its own schedule
and timezone via the building_audit_config table (migrations/
006_layer5_llm_usage_and_config.sql) - "give option to change" done at
the DATA level, not hardcoded, so changing a building's audit times is
an UPDATE statement, not a redeploy.

Design: rather than a generic cron-expression parser (overkill for "a
short list of times per day"), this computes the next occurrence of
each configured HH:MM directly in that building's own timezone - simple
enough to read and trust completely, which matters more here than
generality, since this SQL/logic and the safety-relevant audit cadence
around a fire-risk detection system deserve to be things you can verify
by eye.
"""
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class BuildingAuditSchedule:
    building_id: str
    audit_times: list[str]  # e.g. ["09:00", "13:45", "22:18"], 24h HH:MM, local to timezone_name below
    timezone_name: str      # IANA name, e.g. "Asia/Kolkata"


def _parse_hhmm(value: str) -> dtime:
    hour, minute = value.strip().split(":")
    return dtime(hour=int(hour), minute=int(minute))


def next_audit_time(schedule: BuildingAuditSchedule, now_utc: datetime) -> datetime:
    """
    Returns the next scheduled audit datetime (in UTC) strictly after
    now_utc, across all of this building's configured audit_times.
    Handles the day-rollover case correctly (e.g. if now is 23:00 local
    and the times are 09:00/13:45/22:18, the next one is tomorrow's
    09:00, not today's already-passed 22:18).
    """
    tz = ZoneInfo(schedule.timezone_name)
    now_local = now_utc.astimezone(tz)

    candidates = []
    for hhmm in schedule.audit_times:
        t = _parse_hhmm(hhmm)
        candidate_today = now_local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if candidate_today > now_local:
            candidates.append(candidate_today)
        else:
            candidates.append(candidate_today + timedelta(days=1))

    next_local = min(candidates)
    return next_local.astimezone(ZoneInfo("UTC"))


def seconds_until(schedule: BuildingAuditSchedule, now_utc: datetime) -> float:
    return (next_audit_time(schedule, now_utc) - now_utc).total_seconds()
