-- Layer 4 schema: joint_feature_baseline, joint_features, joint_risk_score,
-- and labeled_incidents (future supervised-learning path). Drop into the
-- same migrations/ folder as 001-004 - name it so it runs after
-- 004_derived_metrics.sql (this file's FKs/logic assume derived_metrics
-- already exists).

-- ---- Per-joint residual baseline (separate from Layer 3's R baseline) ----
-- Layer 3's joint_baseline (migrations/004) learns r_nominal_ohms so
-- residual_c can be COMPUTED at all. This table is a second, independent
-- baseline: once residual_c starts flowing (status='ok' rows begin),
-- we still need this joint's own residual NOISE FLOOR (mean/std of a
-- perfectly healthy joint's residual, which should hover near zero but
-- has its own sensor-noise character) before CUSUM against it means
-- anything. Two separate baselines because they answer two separate
-- questions: "what R counts as healthy for this joint" (Layer 3) vs.
-- "how noisy is this joint's residual signal once healthy" (Layer 4).
CREATE TABLE IF NOT EXISTS joint_feature_baseline (
    panel_id           TEXT             NOT NULL,
    breaker_id         TEXT             NOT NULL,
    reading_count      INTEGER          NOT NULL DEFAULT 0,
    residual_sum       DOUBLE PRECISION NOT NULL DEFAULT 0,
    residual_sumsq     DOUBLE PRECISION NOT NULL DEFAULT 0,
    baseline_mean      DOUBLE PRECISION,
    baseline_std       DOUBLE PRECISION,
    baseline_complete  BOOLEAN          NOT NULL DEFAULT FALSE,
    updated_at         TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (panel_id, breaker_id)
);

-- ---- Persistent EWMA/CUSUM state + processing watermark ----
-- One row per joint. This is what makes feature_processor.py's
-- per-poll-cycle updates a streaming O(1) operation per new reading
-- instead of recomputing EWMA/CUSUM over that joint's entire history
-- every time - the running state IS the memory. last_processed_ts is
-- also the watermark app/feature_service.py's poller uses to find only
-- NEW derived_metrics rows for this joint since the last cycle (see
-- that file's docstring for why polling was chosen over a Layer
-- 3-published Redis event for this pilot).
CREATE TABLE IF NOT EXISTS joint_feature_state (
    panel_id            TEXT             NOT NULL,
    breaker_id          TEXT             NOT NULL,
    ewma_r_effective     DOUBLE PRECISION,
    ewma_residual        DOUBLE PRECISION,
    cusum_value          DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_processed_ts    TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (panel_id, breaker_id)
);

-- ---- Per-reading feature audit trail ----
-- Not read back by the live pipeline (which uses joint_feature_state's
-- running values) - this exists so (a) anyone auditing a specific
-- alert later can see the exact feature trajectory that produced it,
-- and (b) it's the training data source for anomaly_model.py's
-- IsolationForest and any future supervised model.
CREATE TABLE IF NOT EXISTS joint_features (
    ts               TIMESTAMPTZ      NOT NULL,
    panel_id         TEXT             NOT NULL,
    breaker_id       TEXT             NOT NULL,
    ewma_r_effective  DOUBLE PRECISION,
    ewma_residual     DOUBLE PRECISION,
    cusum_value       DOUBLE PRECISION,
    hour_of_day       SMALLINT,
    i_rms             DOUBLE PRECISION,
    computed_at       TIMESTAMPTZ      NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'joint_features',
    by_range('ts', INTERVAL '1 day'),
    if_not_exists => TRUE
);

ALTER TABLE joint_features DROP CONSTRAINT IF EXISTS uq_joint_features_reading;
ALTER TABLE joint_features ADD CONSTRAINT uq_joint_features_reading UNIQUE (panel_id, breaker_id, ts);

CREATE INDEX IF NOT EXISTS idx_joint_features_breaker_time
    ON joint_features (panel_id, breaker_id, ts DESC);

-- ---- Risk score output ----
-- One row per POLL CYCLE per joint (not per raw reading - the slope/
-- anomaly-score/projection calc only re-runs on feature_service.py's
-- polling cadence, see that file), holding everything Layer 5 needs to
-- write a plain-language alert without touching raw sensor data at all.
CREATE TABLE IF NOT EXISTS joint_risk_score (
    ts                          TIMESTAMPTZ      NOT NULL,
    panel_id                    TEXT             NOT NULL,
    breaker_id                  TEXT             NOT NULL,
    anomaly_score               DOUBLE PRECISION,  -- IsolationForest score; more negative = more anomalous. NULL until enough history to train.
    cusum_value                 DOUBLE PRECISION,
    dr_dt_ohms_per_day          DOUBLE PRECISION,  -- rolling 7-day slope of ewma_r_effective
    critical_r_ohms             DOUBLE PRECISION,  -- this joint's own computed critical-resistance threshold, see app/feature_math.py
    projected_days_to_critical  DOUBLE PRECISION,  -- NULL if not degrading (dR/dt <= 0)
    severity_band               TEXT             NOT NULL,  -- healthy | minor | intermediate | serious | emergency | baselining | unknown
    status                      TEXT             NOT NULL,  -- 'baselining' | 'scored' | 'insufficient_history_for_slope' | 'feature_failed'
    error_detail                TEXT,
    computed_at                 TIMESTAMPTZ      NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'joint_risk_score',
    by_range('ts', INTERVAL '1 day'),
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_joint_risk_score_breaker_time
    ON joint_risk_score (panel_id, breaker_id, ts DESC);

-- Fast lookup for "what needs Layer 5's attention right now" - Layer 5
-- polls/queries this instead of scanning the whole table.
CREATE INDEX IF NOT EXISTS idx_joint_risk_score_severity
    ON joint_risk_score (severity_band, ts DESC)
    WHERE severity_band IN ('intermediate', 'serious', 'emergency');

-- ---- Future supervised-learning path (schema only, per requirement #5) ----
-- Populated manually today (a facility manager or technician logs a
-- confirmed "found and fixed a loose joint here on this date" event,
-- e.g. via a Layer 5/6 maintenance-ticket integration once that exists).
-- Once enough rows accumulate, app/anomaly_model.py's retrain job can
-- label the N days of joint_features preceding each incident_date as
-- "pre-failure" and move from unsupervised IsolationForest scoring
-- toward a supervised time-to-critical regression model (e.g. LightGBM)
-- - see anomaly_model.py's module docstring for exactly how that
-- labeling would work. Nothing reads this table yet; it just needs to
-- exist so incidents can start being logged now, before the volume of
-- confirmed incidents makes retraining worthwhile.
CREATE TABLE IF NOT EXISTS labeled_incidents (
    id               BIGSERIAL PRIMARY KEY,
    panel_id         TEXT        NOT NULL,
    breaker_id       TEXT        NOT NULL,
    incident_date    DATE        NOT NULL,
    description      TEXT,               -- e.g. "found loose lug on brk-04, retorqued to spec"
    confirmed_by     TEXT,               -- technician/facility manager name or ID
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_labeled_incidents_joint
    ON labeled_incidents (panel_id, breaker_id, incident_date);
