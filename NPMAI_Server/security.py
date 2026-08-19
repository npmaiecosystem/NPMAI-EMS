import hashlib
import time
from dataclasses import dataclass
from typing import Optional

import asyncpg


def hash_api_key(key: str) -> str:
    # SHA-256, not bcrypt/argon2: device API keys are generated as
    # high-entropy random tokens (secrets.token_urlsafe(32) in
    # scripts/provision_device.py), not human-chosen passwords. Slow
    # hashes exist to defend against brute-forcing low-entropy secrets;
    # a 256-bit random token doesn't need that, and a fast hash keeps
    # this check cheap on the hot request path.
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    building_id: str
    panel_id: str
    api_key_hash: str
    is_active: bool


class DeviceCache:
    """
    In-memory snapshot of the `devices` table, refreshed periodically by
    a background task (see main.py's device_cache_refresh_loop). Both the
    HTTPS auth dependency and the MQTT message handler read from this -
    a DB round trip per inbound reading would put a blocking call
    directly in the ingestion hot path, which requirement #4 rules out.
    At pilot scale (dozens to low thousands of devices) the whole table
    fits comfortably in memory.

    Trade-off: a newly-provisioned or just-deactivated device takes up to
    device_cache_refresh_seconds (default 30s) to take effect. For a
    fleet of physical devices that doesn't change composition
    minute-to-minute, that lag is a reasonable price for keeping the hot
    path DB-free.
    """

    def __init__(self) -> None:
        self._by_device_id: dict[str, DeviceRecord] = {}
        self._by_api_key_hash: dict[str, DeviceRecord] = {}
        self._last_refresh: float = 0.0

    async def refresh(self, pool: asyncpg.Pool) -> None:
        rows = await pool.fetch(
            "SELECT device_id, building_id, panel_id, api_key_hash, is_active FROM devices"
        )
        by_id: dict[str, DeviceRecord] = {}
        by_hash: dict[str, DeviceRecord] = {}
        for row in rows:
            record = DeviceRecord(
                device_id=row["device_id"],
                building_id=row["building_id"],
                panel_id=row["panel_id"],
                api_key_hash=row["api_key_hash"],
                is_active=row["is_active"],
            )
            by_id[record.device_id] = record
            if record.is_active:
                by_hash[record.api_key_hash] = record
        self._by_device_id = by_id
        self._by_api_key_hash = by_hash
        self._last_refresh = time.monotonic()

    def get_by_device_id(self, device_id: str) -> Optional[DeviceRecord]:
        return self._by_device_id.get(device_id)

    def get_by_api_key_hash(self, api_key_hash: str) -> Optional[DeviceRecord]:
        return self._by_api_key_hash.get(api_key_hash)

    @property
    def size(self) -> int:
        return len(self._by_device_id)