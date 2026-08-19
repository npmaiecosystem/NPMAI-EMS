"""
Layer 3 entrypoint: a standalone asyncio process (NOT bundled into the
Layer 2 FastAPI app) that reads new raw_telemetry events off the same
Redis Stream Layer 2 already publishes to (app/redis_events.py's
TelemetryEventPublisher), computes derived physics metrics, and writes
them to derived_metrics.

---- Why a separate process instead of a FastAPI background task ----

Layer 2's whole design center is "ingestion must be the fastest/dumbest
path in the system" (see app/batch_writer.py's docstring). Physics
processing is the opposite: CoolProp/TESPy/PyPSA calls are CPU-bound and
occasionally slow, or fail, to converge (requirement #7 exists because
of this). Running that inside the ingestion service's own event loop
would risk stalling MQTT message handling and HTTPS request handling
behind a misbehaving physics calculation - exactly the coupling Layer 2
was built to avoid. A separate process:

  - can never block ingestion, no matter how a TESPy solve behaves
  - can be scaled independently (more physics workers under load)
    without touching the ingestion service at all
  - can be redeployed independently when the physics libraries need
    updating, without an ingestion outage
  - reuses the SAME Redis consumer group ("physics-service") Layer 2
    already provisions via TelemetryEventPublisher.ensure_consumer_group()
    - that name was already reserved for exactly this purpose (see
      app/redis_events.py's docstring on horizontally scaling "physics
      workers in the same consumer group").

---- Delivery / crash semantics ----

XREADGROUP gives at-least-once delivery: a message stays "pending"
(assigned to this consumer, unacked) until app/physics_processor.py
finishes and we XACK it. If this process crashes mid-batch, those
messages are NOT lost - they sit in the consumer group's pending entries
list until XAUTOCLAIM reclaims them. We run that reclaim once at
startup, covering the exact crash-recovery case app/redis_events.py's
docstring already promises via XPENDING/XCLAIM. Every message also goes
through app/physics_processor.py's dedup check
(derived_metrics_exists), so a redelivered message that was actually
already fully processed before the crash is a safe no-op rather than a
double-processed reading.
"""
import asyncio
import json
import logging
import os
import signal

import redis.asyncio as redis

from app.config import settings
from app.db import close_pool, get_pool, init_pool
from app.physics_processor import process_reading

logger = logging.getLogger(__name__)

_CONSUMER_NAME = f"{settings.physics_consumer_name_prefix}-{os.getpid()}"
_BLOCK_MS = 5000
_BATCH_COUNT = 50
# Only reclaim messages idle longer than this - long enough that we're
# not fighting a still-alive, still-working consumer for its own
# in-flight messages.
_CLAIM_IDLE_MS = 60_000


async def _ensure_consumer_group(redis_client: "redis.Redis") -> None:
    try:
        await redis_client.xgroup_create(
            name=settings.redis_stream_name,
            groupname=settings.redis_consumer_group,
            id="$",
            mkstream=True,
        )
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
        # Already exists (created by Layer 2 at ingestion startup, or a
        # previous run of this service) - fine, idempotent.


async def _process_messages(redis_client: "redis.Redis", messages) -> None:
    pool = get_pool()
    for message_id, fields in messages:
        try:
            raw = fields.get("reading", fields.get(b"reading"))
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            reading = json.loads(raw)
            status = await process_reading(pool, reading)
            logger.debug("Processed message_id=%s status=%s", message_id, status)
        except Exception:
            # Mirrors app/mqtt_client.py's _on_message philosophy exactly:
            # no single malformed/unexpected message is allowed to kill
            # the consumer loop. process_reading() already writes a
            # 'physics_failed' row for any failure it can attribute to a
            # specific, parseable reading - the only messages that reach
            # this except block are ones too broken to even parse (e.g.
            # corrupt JSON), which we can't usefully retry either.
            logger.exception("Unhandled error processing stream message_id=%s - acking and moving on", message_id)
        finally:
            try:
                await redis_client.xack(settings.redis_stream_name, settings.redis_consumer_group, message_id)
            except Exception:
                logger.exception(
                    "Failed to XACK message_id=%s - it will be redelivered/reclaimed later; "
                    "process_reading()'s dedup check makes that safe",
                    message_id,
                )


async def _reclaim_stale_pending(redis_client: "redis.Redis") -> None:
    """Best-effort crash recovery: claim messages that were delivered to
    a (possibly now-dead) consumer in our group but never acked, and
    that have been sitting idle for a while. Failures here are logged
    and swallowed - this is a nice-to-have on startup, not something that
    should block the service from starting at all; any genuinely stuck
    pending messages just get picked up on the next restart instead."""
    try:
        cursor = "0-0"
        while True:
            result = await redis_client.xautoclaim(
                name=settings.redis_stream_name,
                groupname=settings.redis_consumer_group,
                consumername=_CONSUMER_NAME,
                min_idle_time=_CLAIM_IDLE_MS,
                start_id=cursor,
                count=_BATCH_COUNT,
            )
            cursor, messages, *_rest = result
            if not messages:
                break
            logger.info(
                "Reclaimed %d stale pending message(s) from consumer group '%s'",
                len(messages), settings.redis_consumer_group,
            )
            await _process_messages(redis_client, messages)
            if cursor in (b"0-0", "0-0"):
                break
    except Exception:
        logger.exception(
            "Startup stale-message reclaim failed (non-fatal, continuing) - any genuinely stuck "
            "pending messages will be picked up on the next service restart instead"
        )


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info(
        "Starting Layer 3 physics processing service as consumer '%s' in group '%s'",
        _CONSUMER_NAME, settings.redis_consumer_group,
    )

    await init_pool()
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)

    await _ensure_consumer_group(redis_client)
    await _reclaim_stale_pending(redis_client)

    stop_event = asyncio.Event()

    def _handle_stop(*_args):
        logger.info("Shutdown signal received, finishing in-flight batch and exiting...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)
        except NotImplementedError:
            pass  # not supported on this platform - not a deployment target here

    try:
        while not stop_event.is_set():
            try:
                response = await redis_client.xreadgroup(
                    groupname=settings.redis_consumer_group,
                    consumername=_CONSUMER_NAME,
                    streams={settings.redis_stream_name: ">"},
                    count=_BATCH_COUNT,
                    block=_BLOCK_MS,
                )
            except Exception:
                logger.exception("XREADGROUP failed (Redis unreachable?) - retrying shortly")
                await asyncio.sleep(2.0)
                continue

            if not response:
                continue  # block timeout, nothing new - loop back and check stop_event

            for _stream_name, messages in response:
                await _process_messages(redis_client, messages)
    finally:
        logger.info("Shutting down Layer 3 physics processing service...")
        await redis_client.aclose()
        await close_pool()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()