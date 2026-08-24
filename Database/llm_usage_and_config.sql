-- Layer 5 schema: llm_usage (our own cost tracking, no LiteLLM),
-- building_audit_config (configurable scheduled-audit times per
-- building), layer5_outputs (audit/alert results, what Layer 6 polls).

CREATE TABLE IF NOT EXISTS llm_usage (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    building_id    TEXT        NOT NULL,
    purpose        TEXT        NOT NULL,  -- 'scheduled_audit' | 'event_alert' | 'chat_console'
    provider       TEXT        NOT NULL,
    model          TEXT        NOT NULL,
    input_tokens   INTEGER     NOT NULL,
    output_tokens  INTEGER     NOT NULL,
    cost_usd       DOUBLE PRECISION  -- NULL if no pricing entry existed at call time, see app/llm/pricing.py
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_building_date
    ON llm_usage (building_id, ts);

-- Per-building audit schedule - "give option to change" implemented as
-- data, not code. Defaults match the shared architecture spec
-- (09:00/13:45/22:18); insert/update a row per building to override.
CREATE TABLE IF NOT EXISTS building_audit_config (
    building_id    TEXT PRIMARY KEY,
    audit_times    TEXT[]      NOT NULL DEFAULT ARRAY['09:00', '13:45', '22:18'],
    timezone_name  TEXT        NOT NULL DEFAULT 'UTC',  -- IANA name, e.g. 'Asia/Kolkata' - set per building's real location
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What Layer 6 (alert dispatch) polls for new notifications to send,
-- and what Layer 7 (app/website) reads for the alert feed / audit
-- history. panel_id/breaker_id are NULL for a building-wide scheduled
-- audit summary, populated for a single-joint event alert.
CREATE TABLE IF NOT EXISTS layer5_outputs (
    id                   BIGSERIAL PRIMARY KEY,
    ts                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    building_id          TEXT        NOT NULL,
    panel_id             TEXT,
    breaker_id           TEXT,
    source               TEXT        NOT NULL,  -- 'scheduled_audit' | 'event_alert'
    title                TEXT        NOT NULL,
    severity             TEXT        NOT NULL,
    explanation          TEXT        NOT NULL,
    recommended_action   TEXT        NOT NULL,
    confidence           DOUBLE PRECISION NOT NULL,
    dispatched           BOOLEAN     NOT NULL DEFAULT FALSE  -- Layer 6 sets this once it has sent/processed this row
);

CREATE INDEX IF NOT EXISTS idx_layer5_outputs_pending
    ON layer5_outputs (building_id, ts) WHERE NOT dispatched;
