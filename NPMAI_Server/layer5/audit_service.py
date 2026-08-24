"""
Layer 5's audit entrypoint - standalone process, same pattern as
physics_service.py and feature_service.py (never shares an event loop
with ingestion or anything latency-sensitive, since LLM calls are slow
and occasionally fail/retry).

Two triggers, run from the same process:
  1. Scheduled: per-building audit at that building's own configured
     times (app/layer5/audit_scheduler.py), covering ALL joints.
  2. Event-triggered: polls joint_risk_score (same polling pattern as
     Layer 4, for the same reason - no new Redis event exists to
     subscribe to yet) for any joint whose severity_band just crossed
     into 'intermediate' or above since last checked, and fires an
     immediate single-joint alert without waiting for the next
     scheduled audit.
"""
import asyncio
import logging
import signal
from datetime import datetime, timezone

from app.config import settings
from app.db import close_pool, get_pool, init_pool
from app.layer5.audit_scheduler import BuildingAuditSchedule, seconds_until
from app.layer5.output_schema import parse_diagnostic_output
from app.layer5.prompt_templates import AUDIT_SYSTEM_PROMPT
from app.layer5.retrieval import build_audit_context, context_to_prompt_json
from app.llm.base import LLMError
from app.llm.registry import generate_with_failover
from app.llm.usage_tracker import SpendCapExceeded, check_spend_cap, record_usage

logger = logging.getLogger(__name__)

_EVENT_POLL_INTERVAL_SECONDS = 60.0


async def _get_building_schedules(pool) -> list[BuildingAuditSchedule]:
    rows = await pool.fetch("SELECT building_id, audit_times, timezone_name FROM building_audit_config")
    if not rows:
        # No buildings configured yet - not an error, just nothing to
        # schedule. Falls through to defaults if you add a building
        # config row later; this service doesn't need a restart to pick
        # up a new/changed schedule since it reloads this list every
        # cycle (see run()).
        return []
    return [BuildingAuditSchedule(r["building_id"], r["audit_times"], r["timezone_name"]) for r in rows]


async def run_audit_for_building(pool, building_id: str) -> None:
    try:
        await check_spend_cap(pool, building_id)
    except SpendCapExceeded as exc:
        logger.warning("Skipping audit for building=%s: %s", building_id, exc)
        return

    context = await build_audit_context(pool, building_id)
    context_json = context_to_prompt_json(context)

    try:
        response = await generate_with_failover(
            system_prompt=AUDIT_SYSTEM_PROMPT,
            user_prompt=f"Current joint risk data:\n{context_json}",
            max_output_tokens=settings.layer5_max_output_tokens,
        )
    except LLMError:
        logger.exception("All LLM providers failed for building=%s audit - skipping this cycle", building_id)
        return

    await record_usage(pool, building_id, purpose="scheduled_audit", response=response)

    parsed = parse_diagnostic_output(response.text)
    if parsed is None:
        logger.warning("Audit for building=%s produced unparseable output, discarding this cycle's result", building_id)
        return

    await pool.execute(
        """
        INSERT INTO layer5_outputs (building_id, panel_id, breaker_id, source, title, severity, explanation, recommended_action, confidence)
        VALUES ($1, NULL, NULL, 'scheduled_audit', $2, $3, $4, $5, $6)
        """,
        building_id, parsed.title, parsed.severity, parsed.plain_language_explanation,
        parsed.recommended_action, parsed.confidence,
    )
    logger.info("Audit complete for building=%s: severity=%s", building_id, parsed.severity)


async def _check_event_triggers(pool, seen_severities: dict) -> None:
    """
    seen_severities: mutable dict of {(panel_id, breaker_id): last_seen_severity_band},
    kept in-process (not persisted) - a service restart re-detecting
    an already-alerted joint's CURRENT severity as "new" is an
    acceptable, harmless duplicate on restart, versus the complexity of
    persisting this across restarts for a pilot at this scale.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (rs.panel_id, rs.breaker_id)
            rs.panel_id, rs.breaker_id, rs.severity_band, rs.ts, d.building_id
        FROM joint_risk_score rs
        JOIN devices d ON d.panel_id = rs.panel_id
        WHERE rs.severity_band IN ('intermediate', 'serious', 'emergency')
        ORDER BY rs.panel_id, rs.breaker_id, rs.ts DESC
        """
    )
    for row in rows:
        key = (row["panel_id"], row["breaker_id"])
        previous = seen_severities.get(key)
        if previous == row["severity_band"]:
            continue  # no change since last check - don't re-alert on an unchanged, already-known severity
        seen_severities[key] = row["severity_band"]
        await _fire_event_alert(pool, row["building_id"], row["panel_id"], row["breaker_id"])


async def _fire_event_alert(pool, building_id: str, panel_id: str, breaker_id: str) -> None:
    try:
        await check_spend_cap(pool, building_id)
    except SpendCapExceeded as exc:
        logger.warning("Skipping event alert for %s/%s: %s", panel_id, breaker_id, exc)
        return

    row = await pool.fetchrow(
        """
        SELECT rs.severity_band, rs.cusum_value, rs.dr_dt_ohms_per_day, rs.projected_days_to_critical, rs.ts
        FROM joint_risk_score rs WHERE panel_id = $1 AND breaker_id = $2 ORDER BY ts DESC LIMIT 1
        """,
        panel_id, breaker_id,
    )
    import json
    context_json = json.dumps([{
        "panel_id": panel_id, "breaker_id": breaker_id, "severity": row["severity_band"],
        "cusum": row["cusum_value"], "resistance_drift_ohms_per_day": row["dr_dt_ohms_per_day"],
        "projected_days_to_critical": row["projected_days_to_critical"],
        "last_updated": row["ts"].isoformat(),
    }])

    try:
        response = await generate_with_failover(
            system_prompt=AUDIT_SYSTEM_PROMPT,
            user_prompt=f"Current joint risk data:\n{context_json}",
            max_output_tokens=settings.layer5_max_output_tokens,
        )
    except LLMError:
        logger.exception("All LLM providers failed for event alert %s/%s", panel_id, breaker_id)
        return

    await record_usage(pool, building_id, purpose="event_alert", response=response)
    parsed = parse_diagnostic_output(response.text)
    if parsed is None:
        logger.warning("Event alert for %s/%s produced unparseable output, discarding", panel_id, breaker_id)
        return

    await pool.execute(
        """
        INSERT INTO layer5_outputs (building_id, panel_id, breaker_id, source, title, severity, explanation, recommended_action, confidence)
        VALUES ($1, $2, $3, 'event_alert', $4, $5, $6, $7, $8)
        """,
        building_id, panel_id, breaker_id, parsed.title, parsed.severity,
        parsed.plain_language_explanation, parsed.recommended_action, parsed.confidence,
    )
    logger.info("Event alert fired for %s/%s: severity=%s", panel_id, breaker_id, parsed.severity)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("Starting Layer 5 audit service")

    pool = await init_pool()
    seen_severities: dict = {}
    stop_event = asyncio.Event()

    def _handle_stop(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)
        except NotImplementedError:
            pass

    try:
        while not stop_event.is_set():
            await _check_event_triggers(pool, seen_severities)

            schedules = await _get_building_schedules(pool)
            now = datetime.now(timezone.utc)
            for schedule in schedules:
                if seconds_until(schedule, now) <= _EVENT_POLL_INTERVAL_SECONDS:
                    # Close enough to this building's next scheduled
                    # time that it'll fire within our next poll cycle -
                    # run it now rather than risk a multi-minute skew
                    # from waiting for an exact-second match we might miss.
                    logger.info("Running scheduled audit for building=%s", schedule.building_id)
                    await run_audit_for_building(pool, schedule.building_id)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_EVENT_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        await close_pool()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
