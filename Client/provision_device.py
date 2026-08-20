#!/usr/bin/env python3
"""
Provision a new ESP32 device: generates its HTTPS API key + MQTT
credential, registers it in the `devices` table, and creates its MQTT
login in EMQX. Run this once per physical device before it goes into
service (or again to rotate an existing device's credentials).

Usage (with the stack already running via `docker compose up`):

    docker compose run --rm ingestion python scripts/provision_device.py \\
        --device-id esp32-2F-01 --building-id bldg01 --panel-id panel-2F-main

Prints the plaintext API key and MQTT password ONCE - copy them into
that device's config.h (Layer 1 firmware: WIFI/MQTT credentials and the
HTTPS Authorization header if it ever needs the fallback path) right
now. Neither is recoverable afterward: the API key is stored only as a
SHA-256 hash in Postgres, and the MQTT password lives only inside EMQX's
own built-in credential store.
"""
import argparse
import asyncio
import hashlib
import os
import secrets
import sys

import asyncpg
import httpx

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://npmai:npmai_dev_password@timescaledb:5432/npmai")
EMQX_API_URL = os.environ.get("EMQX_API_URL", "http://emqx:18083/api/v5")
EMQX_DASHBOARD_USERNAME = os.environ.get("EMQX_DASHBOARD_USERNAME", "admin")
EMQX_DASHBOARD_PASSWORD = os.environ.get("EMQX_DASHBOARD_PASSWORD", "npmai_dev_admin")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def register_device(device_id: str, building_id: str, panel_id: str, api_key: str) -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            """
            INSERT INTO devices (device_id, building_id, panel_id, api_key_hash, mqtt_username, is_active)
            VALUES ($1, $2, $3, $4, $1, TRUE)
            ON CONFLICT (device_id) DO UPDATE
                SET building_id  = EXCLUDED.building_id,
                    panel_id     = EXCLUDED.panel_id,
                    api_key_hash = EXCLUDED.api_key_hash,
                    is_active    = TRUE
            """,
            device_id, building_id, panel_id, sha256_hex(api_key),
        )
    finally:
        await conn.close()


def create_mqtt_credential(device_id: str, mqtt_password: str) -> None:
    with httpx.Client(timeout=10) as client:
        resp = client.post(f"{EMQX_API_URL}/login", json={
            "username": EMQX_DASHBOARD_USERNAME,
            "password": EMQX_DASHBOARD_PASSWORD,
        })
        resp.raise_for_status()
        token = resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.post(
            f"{EMQX_API_URL}/authentication/password_based:built_in_database/users",
            headers=headers,
            json={"user_id": device_id, "password": mqtt_password},
        )
        if resp.status_code == 409:
            # Credential rotation for an already-provisioned device.
            resp = client.put(
                f"{EMQX_API_URL}/authentication/password_based:built_in_database/users/{device_id}",
                headers=headers,
                json={"password": mqtt_password},
            )
        resp.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device-id", required=True, help="Must match DEVICE_ID in the device's Layer 1 config.h")
    parser.add_argument("--building-id", required=True)
    parser.add_argument("--panel-id", required=True, help="Must match PANEL_ID in the device's Layer 1 config.h")
    args = parser.parse_args()

    api_key = secrets.token_urlsafe(32)
    mqtt_password = secrets.token_urlsafe(24)

    try:
        asyncio.run(register_device(args.device_id, args.building_id, args.panel_id, api_key))
        create_mqtt_credential(args.device_id, mqtt_password)
    except Exception as exc:
        print(f"ERROR: provisioning failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Device provisioned - copy these into the device's config.h NOW ===")
    print(f"  device_id:      {args.device_id}")
    print(f"  building_id:    {args.building_id}")
    print(f"  panel_id:       {args.panel_id}")
    print(f"  HTTPS API key:  {api_key}")
    print(f"                  (send as header: Authorization: Bearer {api_key})")
    print(f"  MQTT username:  {args.device_id}")
    print(f"  MQTT password:  {mqtt_password}")
    print("These are shown once and are not recoverable from the server afterward.\n")


if __name__ == "__main__":
    main()
