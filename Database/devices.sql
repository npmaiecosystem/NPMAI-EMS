-- Layer 2 schema, part 2: per-device authentication.
--
-- Source of truth for both HTTPS API key auth (api_key_hash, checked
-- against the Authorization header) and MQTT identity cross-checking
-- (mqtt_username / panel_id, checked against the connecting device_id
-- claimed in each message's payload - see app/mqtt_client.py). The
-- actual MQTT broker credential (password) is NOT stored here; it lives
-- only in EMQX's own built-in credential store (see
-- scripts/provision_device.py) so a Postgres breach alone can't recover
-- a device's live MQTT password.

CREATE TABLE IF NOT EXISTS devices (
    device_id       TEXT PRIMARY KEY,
    building_id     TEXT NOT NULL,
    panel_id        TEXT NOT NULL,
    api_key_hash    TEXT NOT NULL,          -- SHA-256 hex digest of the HTTPS bearer API key; plaintext key is never stored server-side
    mqtt_username   TEXT NOT NULL UNIQUE,   -- matches device_id by convention; kept as its own column in case that convention ever changes
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_devices_panel ON devices (panel_id);
CREATE INDEX IF NOT EXISTS idx_devices_active ON devices (is_active) WHERE is_active;