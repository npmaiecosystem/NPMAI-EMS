CREATE TABLE IF NOT EXISTS users (
    id             SERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    building_id    TEXT NOT NULL,
    building_name  TEXT NOT NULL DEFAULT 'My Building',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
