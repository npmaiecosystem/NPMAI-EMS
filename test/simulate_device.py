#!/usr/bin/env python3
"""
Simulates one ESP32 device publishing/POSTing telemetry, so you can
verify the Layer 2 ingestion pipeline end-to-end before any real
hardware exists.

Prerequisites: the stack is running (`docker compose up`) and this
device has already been provisioned via scripts/provision_device.py,
which prints the credentials this script needs.

Examples
--------
MQTT mode:
    python test/simulate_device.py mqtt \\
        --host localhost --port 1883 \\
        --mqtt-username esp32-2F-01 --mqtt-password <printed by provision_device.py> \\
        --building-id bldg01 --panel-id panel-2F-main --breaker-id brk-01 \\
        --device-id esp32-2F-01 --count 20 --interval 2

HTTPS fallback mode:
    python test/simulate_device.py http \\
        --url http://localhost:8000 \\
        --api-key <printed by provision_device.py> \\
        --building-id bldg01 --panel-id panel-2F-main --breaker-id brk-01 \\
        --device-id esp32-2F-01 --count 20 --interval 2
"""
import argparse
import json
import random
import threading
import time

import httpx


def make_reading(device_id: str, panel_id: str, breaker_id: str,
                  base_i: float, drift_step: int) -> dict:
    # Mimics a slowly worsening loose joint rather than a flat line: I_rms
    # wanders around a baseline load, and T_hotspot creeps a little
    # further above ambient with each simulated reading, so an end-to-end
    # test run can watch a (fake) trend develop across --count readings -
    # useful once Layer 4's EWMA/CUSUM is in place to confirm it actually
    # reacts to drift, not just to a single high value.
    i_rms = base_i + random.uniform(-0.5, 0.5)
    t_ambient = 24.0 + random.uniform(-0.3, 0.3)
    t_hotspot = t_ambient + 3.0 + (drift_step * 0.15) + random.uniform(-0.2, 0.2)
    return {
        "device_id": device_id,
        "panel_id": panel_id,
        "breaker_id": breaker_id,
        "timestamp": int(time.time()),
        "I_rms": round(i_rms, 3),
        "T_hotspot": round(t_hotspot, 2),
        "T_ambient": round(t_ambient, 2),
        "wifi_rssi": random.randint(-75, -40),
    }


def run_http(args: argparse.Namespace) -> None:
    url = f"{args.url.rstrip('/')}/ingest/telemetry"
    headers = {"Authorization": f"Bearer {args.api_key}"}
    with httpx.Client(timeout=10) as client:
        for i in range(args.count):
            reading = make_reading(args.device_id, args.panel_id, args.breaker_id, args.base_current, i)
            resp = client.post(url, json=reading, headers=headers)
            print(f"[{i + 1}/{args.count}] POST {resp.status_code}: {reading}")
            if resp.status_code >= 400:
                print(f"    -> {resp.text}")
            time.sleep(args.interval)


def run_mqtt(args: argparse.Namespace) -> None:
    import paho.mqtt.client as mqtt

    topic = f"building/{args.building_id}/panel/{args.panel_id}/breaker/{args.breaker_id}/telemetry"
    connected = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            print(f"Connected to MQTT broker as '{args.mqtt_username}'")
            connected.set()
        else:
            print(f"Connection failed: reason_code={reason_code} "
                  f"(check --mqtt-username/--mqtt-password against what "
                  f"provision_device.py printed)")

    # paho-mqtt 2.x requires callback_api_version as the first positional
    # argument - VERSION2 gives the current (reason_code, properties)
    # callback signatures used below.
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{args.device_id}")
    client.username_pw_set(args.mqtt_username, args.mqtt_password)
    client.on_connect = on_connect
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()

    if not connected.wait(timeout=10):
        print("ERROR: could not connect to MQTT broker within 10s")
        client.loop_stop()
        return

    try:
        for i in range(args.count):
            reading = make_reading(args.device_id, args.panel_id, args.breaker_id, args.base_current, i)
            payload = json.dumps(reading)
            result = client.publish(topic, payload, qos=1)
            result.wait_for_publish(timeout=5)
            print(f"[{i + 1}/{args.count}] PUBLISH {topic}: {reading}")
            time.sleep(args.interval)
    finally:
        client.loop_stop()
        client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--device-id", required=True)
    common.add_argument("--building-id", required=True)
    common.add_argument("--panel-id", required=True)
    common.add_argument("--breaker-id", required=True)
    common.add_argument("--count", type=int, default=10)
    common.add_argument("--interval", type=float, default=2.0)
    common.add_argument("--base-current", type=float, default=18.5)

    http_parser = sub.add_parser("http", parents=[common], help="POST to /ingest/telemetry")
    http_parser.add_argument("--url", default="http://localhost:8000")
    http_parser.add_argument("--api-key", required=True)

    mqtt_parser = sub.add_parser("mqtt", parents=[common], help="Publish over MQTT")
    mqtt_parser.add_argument("--host", default="localhost")
    mqtt_parser.add_argument("--port", type=int, default=1883)
    mqtt_parser.add_argument("--mqtt-username", required=True)
    mqtt_parser.add_argument("--mqtt-password", required=True)

    args = parser.parse_args()
    if args.mode == "http":
        run_http(args)
    else:
        run_mqtt(args)


if __name__ == "__main__":
    main()
