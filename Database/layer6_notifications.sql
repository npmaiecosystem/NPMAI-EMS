-- Layer 6 schema: device_tokens (mobile/desktop app registration) and
-- notification_history (persistent alert feed + dedup source of truth).

CREATE TABLE IF NOT EXISTS device_tokens (
    fcm_token      TEXT        PRIMARY KEY,  -- FCM registration tokens are themselves unique per app-install, safe as a PK
    building_id    TEXT        NOT NULL,
    user_id        TEXT        NOT NULL,
    platform       TEXT        NOT NULL,     -- 'android' | 'ios' | 'desktop_windows' | 'desktop_mac' | 'desktop_linux'
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,  -- set FALSE by the dispatcher on a TOKEN_INVALID response (app/fcm/client.py) - never delete outright, keeps an audit trail of what was once registered
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_device_tokens_building_active
    ON device_tokens (building_id) WHERE is_active;

-- One row per (joint or building-wide audit) x (device it was sent to,
-- or NULL for an in-app-only entry not pushed anywhere). This is BOTH
-- the persistent alert feed the app reads AND the dedup source of
-- truth (app/layer6/dedup.py queries this table directly - there's no
-- separate "last known severity" table, avoiding two sources of truth
-- that could drift apart).
CREATE TABLE IF NOT EXISTS notification_history (
    id                   BIGSERIAL PRIMARY KEY,
    ts                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    layer5_output_id     BIGINT      NOT NULL,  -- FK-by-convention to layer5_outputs.id, not a real FK since layer5_outputs isn't a hypertable-safe target here and cross-schema-version ordering isn't guaranteed - see migrations/007
    building_id          TEXT        NOT NULL,
    panel_id             TEXT,
    breaker_id           TEXT,
    severity             TEXT        NOT NULL,
    title                TEXT        NOT NULL,
    disposition          TEXT        NOT NULL,  -- 'push_high_priority' | 'push_normal' | 'in_app_only' | 'suppress'
    fcm_token            TEXT,                  -- NULL for a suppressed/in-app-only-with-no-devices entry
    push_sent            BOOLEAN     NOT NULL DEFAULT FALSE,
    send_outcome         TEXT,                  -- app/fcm/client.py's FCMSendOutcome value, NULL if never attempted (e.g. IN_APP_ONLY)
    resolved             BOOLEAN     NOT NULL DEFAULT FALSE,  -- app/website "mark as resolved" action (Layer 7) sets this
    resolved_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_notification_history_building_time
    ON notification_history (building_id, ts DESC);

-- Dedup query pattern: "what was the last severity we actually notified
-- about for this specific joint" - see app/layer6/dedup.py.
CREATE INDEX IF NOT EXISTS idx_notification_history_joint_time
    ON notification_history (panel_id, breaker_id, ts DESC)
    WHERE panel_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_notification_history_unresolved
    ON notification_history (building_id, ts DESC) WHERE NOT resolved;
