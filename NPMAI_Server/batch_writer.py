import asyncio
import logging
import time
from typing import List, Optional

import asyncpg

from app.config import settings
from app.models import TelemetryPayload
from app.redis_events import TelemetryEventPublisher

logger = logging.getLogger(__name__)


class BatchWriter:
    """
    The only thing standing between "reading validated" and "reading
    durable" is this queue plus a background flush loop. Both the MQTT
    handler and the HTTPS endpoint call enqueue() and return immediately
    - neither ever awaits a DB round trip. A single background task
    drains the queue in batches, does one batched INSERT, and then fans
    out the corresponding Redis Stream events for whatever it just
    committed. This whole class exists to satisfy requirement #4 exactly:
    the ingestion path is fast/dumb, and batching is how it stays that
    way under load instead of becoming the bottleneck.

    ---- Why batch instead of one INSERT per reading ----

    At this pilot's scale (~60 breakers x 1 reading/60s = roughly 1
    message/sec averaged across the whole building), a naive
    one-INSERT-per-reading approach would already keep up without any
    trouble. This design is meant to not need a rewrite as the
    deployment grows, though: even at 50 buildings of this size (~3,000
    breakers, ~50 msg/sec average, with real bursts well above that
    since devices' independent 60s send timers tend to drift into rough
    alignment over time), a per-row INSERT round trip to Postgres
    (network latency + query planning + WAL fsync, per statement) becomes
    the bottleneck long before CPU or network bandwidth do. Batching many
    rows into one statement amortizes all of that fixed per-statement
    cost across N rows instead of paying it N times.

    ---- Why asyncpg.executemany(), and NOT COPY, even though COPY is faster ----

    COPY (asyncpg's copy_records_to_table) is the fastest bulk-load path
    Postgres offers, but it is a raw bulk loader: it does not support ON
    CONFLICT, and it aborts the ENTIRE batch on any constraint violation.
    We need ON CONFLICT DO NOTHING here because MQTT QoS 1 is an
    at-least-once delivery guarantee - a broker/network hiccup can
    legitimately redeliver a message we already wrote, and COPY would
    turn that into a batch-wide failure instead of a harmless no-op.
    executemany() pipelines every row of a batch over the wire as one
    round trip (NOT N separate round trips - asyncpg batches the
    protocol messages), which is fast enough for this workload while
    staying idempotent via the (panel_id, breaker_id, ts) unique
    constraint (see migrations/001_extension_and_hypertable.sql). If a
    future deployment's scale genuinely outgrows this, the move is a
    staging-table COPY plus a periodic dedup/upsert pass - not a rewrite
    of this class's public interface.

    ---- Reliability trade-off (documented, not hidden) ----

    The queue is in memory. A hard crash of this process between
    enqueue() and the next flush loses whatever was currently queued -
    bounded by batch_flush_interval_seconds (default 500ms) worth of
    traffic. This mirrors the exact trade-off already made in the ESP32
    firmware's RAM ring buffer (Layer 1): for a system whose entire
    premise is detecting slow, weeks-to-months resistance drift, losing
    a sub-second slice of raw samples on a rare process crash is not a
    correctness problem for Layer 4's trend detection.
    """

    def __init__(self, pool: asyncpg.Pool, event_publisher: TelemetryEventPublisher) -> None:
        self._pool = pool
        self._event_publisher = event_publisher
        self._queue: asyncio.Queue[TelemetryPayload] = asyncio.Queue(maxsize=settings.ingest_queue_max_size)
        self._task: Optional[asyncio.Task] = None
        self.dropped_count = 0
        self.inserted_count = 0

    async def enqueue(self, reading: TelemetryPayload) -> bool:
        """
        Non-blocking. Returns False if the queue is full (DB writes are
        falling behind incoming traffic) - callers decide what that
        means for them: the HTTPS endpoint turns it into a 503 so the
        caller can retry; the MQTT handler logs and drops, since MQTT has
        no synchronous caller waiting on a response to retry against.
        """
        try:
            self._queue.put_nowait(reading)
            return True
        except asyncio.QueueFull:
            self.dropped_count += 1
            logger.error(
                "Ingest queue full (max %d) - dropping reading for panel=%s breaker=%s. "
                "DB writes are falling behind incoming traffic.",
                settings.ingest_queue_max_size, reading.panel_id, reading.breaker_id,
            )
            return False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="batch-writer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            batch = await self._collect_batch()
            if batch:
                await self._flush(batch)

    async def _collect_batch(self) -> List[TelemetryPayload]:
        """
        Collects up to batch_max_size items, but never waits longer than
        batch_flush_interval_seconds total for the batch to fill - so a
        quiet period doesn't leave a handful of readings sitting
        unflushed indefinitely, and a busy period doesn't grow a batch
        without bound.
        """
        batch: List[TelemetryPayload] = []
        deadline = time.monotonic() + settings.batch_flush_interval_seconds
        while len(batch) < settings.batch_max_size:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                batch.append(item)
            except asyncio.TimeoutError:
                break
        return batch

    async def _flush(self, batch: List[TelemetryPayload]) -> None:
        records = [
            (
                r.device_id, r.panel_id, r.breaker_id,
                r.timestamp,  # converted to timestamptz by to_timestamp() below
                r.I_rms, r.T_hotspot, r.T_ambient, r.wifi_rssi,
            )
            for r in batch
        ]
        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO raw_telemetry
                        (ts, device_id, panel_id, breaker_id, i_rms, t_hotspot, t_ambient, wifi_rssi)
                    VALUES
                        (to_timestamp($4), $1, $2, $3, $5, $6, $7, $8)
                    ON CONFLICT (panel_id, breaker_id, ts) DO NOTHING
                    """,
                    records,
                )
        except Exception:
            logger.exception(
                "Batch insert of %d reading(s) failed - these readings are "
                "lost (they only ever existed in the in-memory queue). "
                "Investigate TimescaleDB connectivity/health.",
                len(batch),
            )
            return

        self.inserted_count += len(batch)
        await self._event_publisher.publish_batch(batch)