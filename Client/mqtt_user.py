#!/usr/bin/env python3
"""
One-shot startup task (run by the `emqx-init` service in
docker-compose.yml): ensures EMQX's built_in_database password
authenticator has a credential for the ingestion service's OWN MQTT
subscriber connection. Idempotent - safe to run on every `docker compose
up`.

This is deliberately separate from scripts/provision_device.py: this
script provisions the ONE privileged subscriber identity the ingestion
service itself uses to subscribe to building/+/panel/+/breaker/+/telemetry.
provision_device.py is what you run once per physical ESP32 to give it
its own publish-only credential. Keeping these separate means a
compromised device credential can't be used to subscribe to (i.e. read)
every other device's telemetry.

The built_in_database authenticator itself is enabled declaratively via
environment variables on the emqx service in docker-compose.yml
(EMQX_AUTHENTICATION__1__*) - this script only adds a user to it.
"""
import os
import sys
import time

import httpx

EMQX_API_URL = os.environ.get("EMQX_API_URL", "http://emqx:18083/api/v5")
DASHBOARD_USERNAME = os.environ["EMQX_DASHBOARD_USERNAME"]
DASHBOARD_PASSWORD = os.environ["EMQX_DASHBOARD_PASSWORD"]
MQTT_USERNAME = os.environ["MQTT_INGESTION_USERNAME"]
MQTT_PASSWORD = os.environ["MQTT_INGESTION_PASSWORD"]


def login(client: httpx.Client) -> str:
    resp = client.post(f"{EMQX_API_URL}/login", json={
        "username": DASHBOARD_USERNAME,
        "password": DASHBOARD_PASSWORD,
    })
    resp.raise_for_status()
    return resp.json()["token"]


def main() -> None:
    with httpx.Client(timeout=10) as client:
        token = None
        last_error = None
        for attempt in range(30):
            try:
                token = login(client)
                break
            except (httpx.HTTPError, KeyError) as exc:
                last_error = exc
                print(f"[emqx-init] EMQX dashboard API not ready yet ({exc}), retrying...")
                time.sleep(2)
        if token is None:
            print(f"[emqx-init] ERROR: could not log in to EMQX dashboard after retries: {last_error}",
                  file=sys.stderr)
            sys.exit(1)

        headers = {"Authorization": f"Bearer {token}"}

        resp = client.post(
            f"{EMQX_API_URL}/authentication/password_based:built_in_database/users",
            headers=headers,
            json={"user_id": MQTT_USERNAME, "password": MQTT_PASSWORD},
        )
        if resp.status_code in (200, 201):
            print(f"[emqx-init] Created MQTT user '{MQTT_USERNAME}'")
        elif resp.status_code == 409:
            print(f"[emqx-init] MQTT user '{MQTT_USERNAME}' already exists - updating password to match .env")
            # NOTE: this PUT path follows EMQX's general REST convention
            # (PUT .../users/{user_id} to replace an existing user's
            # fields) but is less thoroughly documented than the POST
            # create path above. If this specific call 404s/fails on
            # your EMQX version, delete the user via the Dashboard
            # (Access Control -> Authentication -> user) and re-run this
            # script so it takes the POST/create path instead.
            resp2 = client.put(
                f"{EMQX_API_URL}/authentication/password_based:built_in_database/users/{MQTT_USERNAME}",
                headers=headers,
                json={"password": MQTT_PASSWORD},
            )
            resp2.raise_for_status()
        else:
            print(f"[emqx-init] ERROR creating MQTT user: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)

        print("[emqx-init] Done.")


if __name__ == "__main__":
    main()