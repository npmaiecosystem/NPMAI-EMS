-- Layer 3 schema: derived_metrics hypertable + joint_baseline tracking
-- table. Drop this file into the same migrations/ folder as
-- 001_extension_and_hypertable.sql - the `migrate` service in
-- docker-compose.yml runs every migrations/*.sql file in filename order,
-- so this runs after raw_telemetry exists (required: derived_metrics
-- carries a FK back to it). Safe to re-run: everything here is
-- idempotent (IF NOT EXISTS / DROP+re-ADD), same convention as 001.

CREATE TABLE IF NOT EXISTS derived_metrics (
    ts                       TIMESTAMPTZ      NOT NULL,
    device_id                TEXT             NOT NULL,
    panel_id                 TEXT             NOT NULL,
    breaker_id               TEXT             NOT NULL,

    r_effective_ohms         DOUBLE PRECISION,  -- back-calculated contact resistance (app/physics/thermal_model.py)
    theta_thermal_k_per_w    DOUBLE PRECISION,  -- intermediate joint-to-ambient thermal resistance, kept for audit/debug
    r_nominal_ohms           DOUBLE PRECISION,  -- this joint's learned healthy resistance at computation time (app/baseline.py)
    t_expected_c             DOUBLE PRECISION,  -- TESPy-solved expected T_hotspot for a healthy joint at this I_rms
    residual_c                DOUBLE PRECISION,  -- T_hotspot(measured) - t_expected_c: the real fault signal
    pypsa_cross_check_ratio  DOUBLE PRECISION,  -- QA-only: (I^2*R_effective) / (PyPSA-computed line loss), nullable

    -- 'ok'               - full pipeline succeeded, residual_c is meaningful
    -- 'baseline_building' - R_effective recorded, but this joint hasn't
    --                       accumulated enough history yet for a
    --                       T_expected/residual to mean anything (requirement #5)
    -- 'low_current_skip' - I_rms below the noise floor for a meaningful
    --                       R_effective back-calc, physics skipped entirely
    -- 'physics_failed'   - a physics library call failed/didn't converge;
    --                      see error_detail (requirement #7)
    status                    TEXT             NOT NULL,
    error_detail              TEXT,

    computed_at                TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- Same 1-day chunking rationale as raw_telemetry (see
-- 001_extension_and_hypertable.sql) - one derived_metrics row per
-- raw_telemetry row, so chunk sizing tracks it exactly.
SELECT create_hypertable(
    'derived_metrics',
    by_range('ts', INTERVAL '1 day'),
    if_not_exists => TRUE
);

-- Idempotency for at-least-once Redis Stream delivery: a consumer crash
-- between processing and XACK can legitimately redeliver an event this
-- service already wrote a row for. app/physics_processor.py checks for
-- an existing row before doing any work (so baseline bookkeeping - not
-- naturally idempotent - doesn't double-count a redelivered sample); this
-- constraint backs that check and makes the upsert itself safe as
-- defense in depth.
ALTER TABLE derived_metrics DROP CONSTRAINT IF EXISTS uq_derived_metrics_reading;
ALTER TABLE derived_metrics ADD CONSTRAINT uq_derived_metrics_reading UNIQUE (panel_id, breaker_id, ts);

CREATE INDEX IF NOT EXISTS idx_derived_metrics_breaker_time
    ON derived_metrics (panel_id, breaker_id, ts DESC);

-- Fast lookup for "what's currently unhealthy in the physics pipeline
-- itself" (ops/debugging query, not a fault signal - 'baseline_building'
-- and 'low_current_skip' are both expected/normal statuses).
CREATE INDEX IF NOT EXISTS idx_derived_metrics_status
    ON derived_metrics (status) WHERE status <> 'ok';

-- FK back to the source raw reading (requirement #3: "linked via
-- foreign key/timestamp"). TimescaleDB supports FKs FROM one hypertable
-- TO another as long as the referenced side has a UNIQUE constraint that
-- includes the partitioning column - raw_telemetry's
-- uq_raw_telemetry_reading (panel_id, breaker_id, ts) qualifies. If your
-- TimescaleDB version rejects cross-hypertable FKs, comment this
-- ALTER TABLE block out: the UNIQUE constraint above still gives
-- idempotent upserts, and (panel_id, breaker_id, ts) is still a correct
-- logical join key against raw_telemetry either way.
ALTER TABLE derived_metrics DROP CONSTRAINT IF EXISTS fk_derived_metrics_raw_telemetry;
ALTER TABLE derived_metrics ADD CONSTRAINT fk_derived_metrics_raw_telemetry
    FOREIGN KEY (panel_id, breaker_id, ts) REFERENCES raw_telemetry (panel_id, breaker_id, ts);

-- Per-joint baseline accumulator (requirement #5): tracks the running
-- count/sum/sum-of-squares of R_effective during the "baseline-building"
-- window (the first `baseline_min_readings` valid readings for that
-- joint - see app/config.py) so this joint's own healthy R_nominal can
-- be learned instead of guessed as a fleet-wide constant. One row per
-- breaker - tiny cardinality even at scale (thousands of breakers, not
-- rows-per-reading) - so a plain table, not a hypertable.
CREATE TABLE IF NOT EXISTS joint_baseline (
    panel_id          TEXT             NOT NULL,
    breaker_id        TEXT             NOT NULL,
    reading_count     INTEGER          NOT NULL DEFAULT 0,
    r_sum             DOUBLE PRECISION NOT NULL DEFAULT 0,
    r_sumsq           DOUBLE PRECISION NOT NULL DEFAULT 0,
    r_nominal_ohms    DOUBLE PRECISION,
    baseline_complete BOOLEAN          NOT NULL DEFAULT FALSE,
    updated_at        TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (panel_id, breaker_id)
);