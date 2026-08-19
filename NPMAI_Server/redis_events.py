import json
import logging
from typing import List

import redis.asyncio as redis

from app.config import settings
from app.models import TelemetryPayload

logger = logging.getLogger(__name__)


class TelemetryEventPublisher:
    """
    Publishes a lightweight event per raw reading to a Redis Stream after
    it has been durably written to TimescaleDB, so Layer 3 (physics
    processing) reacts to new readings without polling the database.

    ---- Why Redis Streams over Postgres LISTEN/NOTIFY ----

    NOTIFY payloads are capped at 8000 bytes and, critically, are
    fire-and-forget: if no listener is connected at the instant NOTIFY
    fires, that notification is gone forever. Layer 3 restarting for a
    deploy, crashing, or just briefly reconnecting would silently lose
    events during that window - which would force Layer 3 to ALSO poll
    the DB as a backstop, defeating the entire point of an event-driven
    handoff.

    Redis Streams persist entries (bounded by MAXLEN, trimmed
    approximately) as an actual append-only log, not a transient signal.
    A consumer group (XREADGROUP) gives Layer 3 at-least-once delivery
    with a durable per-consumer read offset: if it's down for five
    minutes, it resumes exactly where it left off on restart instead of
    missing whatever happened in that window. XPENDING also lets Layer 3
    detect and reclaim events it fetched but crashed before acking -
    which matters for a consumer that's about to do nontrivial physics
    computation (CoolProp/TESPy/PyPSA calls) per event, not just a quick
    handler. It also means Layer 3 can scale horizontally later (multiple
    physics workers in the same consumer group splitting the stream)
    with zero change to this publisher.

    Trade-off accepted: this requires Redis to be up for real-time
    handoff to work, versus zero extra infrastructure for NOTIFY. Given
    Redis is already part of this stack specifically for this purpose,
    and durability/replay matter more here than infra minimalism,
    Streams are the right call. If Redis is briefly down, readings are
    still safe - they're already committed in TimescaleDB by the time we
    publish - Layer 3 just doesn't get real-time notification for that
    gap (see the exception handling in publish_batch below).
    """

    def __init__(self, redis_client: "redis.Redis") -> None:
        self._redis = redis_client

    async def ensure_consumer_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                name=settings.redis_stream_name,
                groupname=settings.redis_consumer_group,
                id="$",
                mkstream=True,
            )
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            # Group already exists from a previous run - fine, idempotent.

    async def publish_batch(self, readings: List[TelemetryPayload]) -> None:
        if not readings:
            return
        pipe = self._redis.pipeline()
        for r in readings:
            event = {
                "device_id": r.device_id,
                "panel_id": r.panel_id,
                "breaker_id": r.breaker_id,
                "timestamp": r.timestamp,
                "I_rms": r.I_rms,
                "T_hotspot": r.T_hotspot,
                "T_ambient": r.T_ambient,
                "wifi_rssi": r.wifi_rssi,
            }
            pipe.xadd(
                settings.redis_stream_name,
                {"reading": json.dumps(event)},
                maxlen=settings.redis_stream_maxlen,
                approximate=True,
            )
        try:
            await pipe.execute()
        except Exception:
            logger.exception(
                "Failed to publish %d event(s) to Redis stream '%s'. The "
                "readings themselves are already durably committed in "
                "TimescaleDB - this only delays real-time notification to "
                "Layer 3, it does not lose data.",
                len(readings), settings.redis_stream_name,
            )