import asyncio
import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI

from app.batch_writer import BatchWriter
from app.config import settings
from app.db import close_pool, init_pool
from app.mqtt_client import TelemetryMQTTSubscriber
from app.redis_events import TelemetryEventPublisher
from app.routes import router
from app.security import DeviceCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def _device_cache_refresh_loop(cache: DeviceCache, pool, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await cache.refresh(pool)
        except Exception:
            logger.exception("Device cache refresh failed - keeping previous snapshot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- Startup ----
    pool = await init_pool()
    app.state.db_pool = pool

    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    app.state.redis_client = redis_client

    device_cache = DeviceCache()
    await device_cache.refresh(pool)  # populate before accepting any traffic
    app.state.device_cache = device_cache
    cache_task = asyncio.create_task(
        _device_cache_refresh_loop(device_cache, pool, settings.device_cache_refresh_seconds),
        name="device-cache-refresh",
    )

    event_publisher = TelemetryEventPublisher(redis_client)
    await event_publisher.ensure_consumer_group()

    batch_writer = BatchWriter(pool, event_publisher)
    batch_writer.start()
    app.state.batch_writer = batch_writer

    mqtt_subscriber = TelemetryMQTTSubscriber(batch_writer, device_cache)
    await mqtt_subscriber.connect()
    app.state.mqtt_subscriber = mqtt_subscriber

    logger.info(
        "Ingestion service ready - %d device(s) loaded, subscribed to '%s'",
        device_cache.size, settings.mqtt_topic_filter,
    )

    try:
        yield
    finally:
        # ---- Shutdown ----
        logger.info("Shutting down ingestion service...")
        cache_task.cancel()
        await mqtt_subscriber.disconnect()
        await batch_writer.stop()
        await redis_client.aclose()
        await close_pool()


app = FastAPI(title="NPMAI Layer 2 - Ingestion Service", lifespan=lifespan)
app.include_router(router)