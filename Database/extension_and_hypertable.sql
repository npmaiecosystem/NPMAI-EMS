-- Layer 2 schema, part 1: raw_telemetry hypertable.
--
-- Applied automatically by the `migrate` service in docker-compose.yml,
-- which runs every migrations/*.sql file in filename order. Safe to
-- re-run: everything here is idempotent (IF NOT EXISTS / DROP+re-ADD).

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS raw_telemetry (
    ts          TIMESTAMPTZ      NOT NULL,
    device_id   TEXT             NOT NULL,
    panel_id    TEXT             NOT NULL,
    breaker_id  TEXT             NOT NULL,
    i_rms       DOUBLE PRECISION,
    t_hotspot   DOUBLE PRECISION,
    t_ambient   DOUBLE PRECISION,
    wifi_rssi   SMALLINT
);

-- One-day chunks: at this pilot's ingestion volume (dozens to low
-- thousands of readings/day per breaker), a 1-day chunk keeps each
-- chunk's index comfortably small without fragmenting into an excessive
-- number of tiny chunks. Revisit (larger interval) only if the fleet
-- grows by orders of magnitude and per-chunk overhead starts to matter.
SELECT create_hypertable(
    'raw_telemetry',
    by_range('ts', INTERVAL '1 day'),
    if_not_exists => TRUE
);

-- Idempotency for at-least-once MQTT delivery (QoS 1): a broker/network
-- hiccup can legitimately redeliver a message the ingestion service
-- already wrote. This constraint is what lets the ingestion service's
-- batched INSERT ... ON CONFLICT DO NOTHING (see app/batch_writer.py)
-- silently de-duplicate a redelivery instead of erroring or double
-- counting. TimescaleDB requires any UNIQUE constraint on a hypertable
-- to include the partitioning column (ts) - it's included here.
ALTER TABLE raw_telemetry DROP CONSTRAINT IF EXISTS uq_raw_telemetry_reading;
ALTER TABLE raw_telemetry ADD CONSTRAINT uq_raw_telemetry_reading UNIQUE (panel_id, breaker_id, ts);

-- Layer 3/4's dominant query pattern is "give me joint X's history over
-- the last N days/weeks for EWMA/CUSUM/trend calculation" - i.e. always
-- scoped to one panel+breaker, ordered by time. This composite index
-- (which also backs the UNIQUE constraint above) serves that directly.
-- TimescaleDB already indexes the partitioning column (ts) on its own,
-- so no separate ts-only index is needed.
CREATE INDEX IF NOT EXISTS idx_raw_telemetry_breaker_time
    ON raw_telemetry (panel_id, breaker_id, ts DESC);

-- Secondary index for device-level debugging/audit queries ("show me
-- everything this specific ESP32 has ever sent").
CREATE INDEX IF NOT EXISTS idx_raw_telemetry_device_time
    ON raw_telemetry (device_id, ts DESC);