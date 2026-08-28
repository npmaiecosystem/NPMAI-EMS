"""
Layer 4 entrypoint: a standalone asyncio process (same pattern as
app/physics_service.py - separate from both the ingestion service and
the physics service, for the identical reason: this must never share an
event loop with a service that has a "stay fast" requirement, and it can
be scaled/redeployed independently).

---- Why polling instead of event-driven off a new Redis stream ----

Layer 3 does not currently publish an event when it writes a
derived_metrics row - it only CONSUMES the telemetry:new stream Layer 2
publishes. Layer 4 needs to know about NEW derived_metrics rows, and the
lowest-risk way to get that without modifying Layer 3's already-reviewed
code is to poll: every feature_poll_interval_seconds, ask "which joints
have derived_metrics rows newer than their own watermark"
(app/feature_state_store.py's get_joints_with_pending_readings) and
process just those.

At this pilot's scale (dozens of breakers, one physics reading per
breaker roughly every 60s) a 60-second poll interval means at most a
~60-120 second delay between a reading landing in derived_metrics and
its risk score being computed - acceptable for a system whose entire
premise is detecting drift over WEEKS. If you want genuinely
event-driven handoff later instead, the change is small and localized:
add one `await event_publisher.publish_batch(...)`-style XADD call at
the end of app/physics_processor.py's _safe_write() (mirroring exactly
what app/redis_events.py's TelemetryEventPublisher already does for
Layer 2->3), then swap this file's poll loop for an XREADGROUP loop
identical in structure to physics_service.py's own. Nothing in
feature_processor.py needs to change either way - it already just takes
a (panel_id, breaker_id) and does the right thing regardless of what
triggered the call.

---- Why per-joint processing, not one giant batch query ----

process_joint() (app/feature_processor.py) does several small,
sequential DB round trips per joint (load state, fetch new readings,
baseline upsert, slope query, theta lookup, write risk row). At pilot
scale (dozens of joints) processing them one at a time each poll cycle
is simple and easy to reason about; if the fleet grows by orders of
magnitude, the natural next step is asyncio.gather() over joints with a
bounded concurrency limit (e.g. a semaphore) rather than a rewrite of
process_joint() itself - the same "extend, don't rewrite the interface"
philosophy already used elsewhere in this project.
"""
import asyncio
import logging
import signal

from app.config import settings
from app.db import close_pool, get_pool, init_pool
from app.feature_processor import process_joint
from app.feature_state_store import get_joints_with_pending_readings

logger = logging.getLogger(__name__)


async def _poll_once() -> None:
    pool = get_pool()
    try:
        joints = await get_joints_with_pending_readings(pool)
    except Exception:
        logger.exception("Failed to query pending joints this cycle - will retry next cycle")
        return

    if not joints:
        return

    logger.debug("Processing %d joint(s) with new readings this cycle", len(joints))
    for panel_id, breaker_id in joints:
        try:
            status = await process_joint(pool, panel_id, breaker_id)
            logger.debug("panel=%s breaker=%s -> %s", panel_id, breaker_id, status)
        except Exception:
            # process_joint() already catches everything internally and
            # writes a 'feature_failed' row - this is a final backstop
            # in case something outside that try block (e.g. the call
            # itself) misbehaves, so one bad joint can never kill the
            # poll loop for every other joint.
            logger.exception("Unexpected error processing panel=%s breaker=%s - continuing with next joint", panel_id, breaker_id)


async def _retrain_all_models_once() -> None:
    """
    Weekly (settings.anomaly_retrain_interval_hours) full re-fit of every
    joint's IsolationForest against its latest accumulated
    joint_features history. Re-fitting from scratch each time (not
    incremental) because IsolationForest doesn't support incremental
    updates, and at this pilot's data volume a full re-fit is cheap
    enough not to need optimizing yet.
    """
    from app.anomaly_model import train_and_save_model

    pool = get_pool()
    try:
        joints = await pool.fetch("SELECT DISTINCT panel_id, breaker_id FROM joint_features")
    except Exception:
        logger.exception("Retrain cycle: failed to list joints - skipping this cycle")
        return

    for row in joints:
        panel_id, breaker_id = row["panel_id"], row["breaker_id"]
        try:
            feature_rows = await pool.fetch(
                """
                SELECT ewma_r_effective, ewma_residual, cusum_value, hour_of_day, i_rms
                FROM joint_features WHERE panel_id = $1 AND breaker_id = $2
                ORDER BY ts DESC LIMIT 5000
                """,
                panel_id, breaker_id,
            )
            train_and_save_model(panel_id, breaker_id, [dict(r) for r in feature_rows])
        except Exception:
            logger.exception("Retrain failed for panel=%s breaker=%s - keeping previous model, if any", panel_id, breaker_id)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("Starting Layer 4 feature/risk service (poll interval=%.0fs)", settings.feature_poll_interval_seconds)

    await init_pool()

    stop_event = asyncio.Event()

    def _handle_stop(*_args):
        logger.info("Shutdown signal received, finishing current cycle and exiting...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)
        except NotImplementedError:
            pass

    retrain_interval_seconds = settings.anomaly_retrain_interval_hours * 3600.0
    seconds_since_retrain = retrain_interval_seconds  # trigger one retrain pass on first startup

    try:
        while not stop_event.is_set():
            await _poll_once()

            seconds_since_retrain += settings.feature_poll_interval_seconds
            if seconds_since_retrain >= retrain_interval_seconds:
                logger.info("Running scheduled anomaly model retrain across all joints")
                await _retrain_all_models_once()
                seconds_since_retrain = 0.0

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.feature_poll_interval_seconds)
            except asyncio.TimeoutError:
                pass  # normal case: just means it's time for the next poll cycle
    finally:
        logger.info("Shutting down Layer 4 feature/risk service...")
        await close_pool()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
