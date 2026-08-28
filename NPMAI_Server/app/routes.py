import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.models import TelemetryPayload
from app.security import DeviceCache, DeviceRecord, hash_api_key

logger = logging.getLogger(__name__)

router = APIRouter()


def get_device_cache(request: Request) -> DeviceCache:
    return request.app.state.device_cache


def get_batch_writer(request: Request):
    return request.app.state.batch_writer


async def verify_api_key(
    authorization: str = Header(..., description="Bearer <device API key>"),
    device_cache: DeviceCache = Depends(get_device_cache),
) -> DeviceRecord:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or malformed Authorization header")
    api_key = authorization.removeprefix("Bearer ").strip()
    if not api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Empty API key")
    device = device_cache.get_by_api_key_hash(hash_api_key(api_key))
    if device is None or not device.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or inactive API key")
    return device


@router.post("/ingest/telemetry", status_code=status.HTTP_202_ACCEPTED)
async def ingest_telemetry(
    payload: TelemetryPayload,
    device: DeviceRecord = Depends(verify_api_key),
    batch_writer=Depends(get_batch_writer),
):
    """
    HTTPS fallback for devices that can't (or shouldn't) speak MQTT.
    Accepts the identical JSON schema as the MQTT telemetry messages;
    same auth philosophy as the MQTT path, just enforced via a bearer
    token instead of a broker-level credential.
    """
    if payload.device_id != device.device_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API key does not match device_id in payload")
    if payload.panel_id != device.panel_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Device is not registered to this panel_id")

    ok = await batch_writer.enqueue(payload)
    if not ok:
        # Unlike the MQTT path (fire-and-forget, nothing to retry
        # against), an HTTPS caller CAN retry - so surface backpressure
        # explicitly instead of silently dropping.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Ingestion queue full, retry shortly")
    return {"status": "accepted"}


@router.get("/health")
async def health(request: Request):
    app = request.app
    db_ok = False
    redis_ok = False

    try:
        async with app.state.db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        logger.exception("Health check: DB unreachable")

    try:
        await app.state.redis_client.ping()
        redis_ok = True
    except Exception:
        logger.exception("Health check: Redis unreachable")

    mqtt_ok = bool(getattr(app.state.mqtt_subscriber.client, "is_connected", False))
    batch_writer = app.state.batch_writer

    healthy = db_ok and redis_ok and mqtt_ok
    return {
        "status": "ok" if healthy else "degraded",
        "database": db_ok,
        "redis": redis_ok,
        "mqtt": mqtt_ok,
        "devices_cached": app.state.device_cache.size,
        "queue_depth": batch_writer._queue.qsize(),
        "inserted_total": batch_writer.inserted_count,
        "dropped_total": batch_writer.dropped_count,
    }