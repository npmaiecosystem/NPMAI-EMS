-- Layer 2 schema, part 3: compression + retention policy for raw_telemetry.
--
-- Raw, single-reading-resolution data does not need to live forever at
-- full resolution or full storage cost - this file encodes exactly how
-- long it lives and how it's stored as it ages.

-- ---- Compression ----
-- Chunks older than 7 days are compressed. Recent data stays
-- uncompressed because Layer 3/4 query it constantly with small,
-- per-joint range scans (and the ingestion service itself is
-- continuously inserting into the current chunk) - compression's
-- columnar format is excellent for large sequential scans over old,
-- "cold" data, but adds decompression overhead to exactly the access
-- pattern recent data sees the most.
--
-- segmentby (panel_id, breaker_id): groups each joint's own time series
-- together within a compressed chunk. This matters twice over: it gives
-- a substantially better compression ratio (one joint's I_rms/T_hotspot
-- values are far more self-similar than the whole panel's interleaved
-- readings would be), AND it lets a compressed-chunk scan for "joint X's
-- last 90 days" skip straight to that joint's segment instead of
-- decompressing the whole chunk to find its rows.
ALTER TABLE raw_telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'panel_id, breaker_id',
    timescaledb.compress_orderby = 'ts DESC'
);

SELECT add_compression_policy('raw_telemetry', INTERVAL '7 days', if_not_exists => TRUE);

-- ---- Retention ----
-- Raw telemetry is dropped entirely after 180 days. This system's whole
-- premise is WEEKS-TO-MONTHS resistance drift (per the physics: I^2*R
-- heating -> thermal cycling -> further loosening, a slow feedback
-- loop), so 180 days of raw history gives ample runway to retrospectively
-- investigate or re-derive any trend Layer 4 flagged, plus margin for
-- slow-moving maintenance-ticket correlation and audits.
--
-- raw_telemetry is intentionally a ROLLING WINDOW, not the system's
-- long-term memory - Layer 4's own derived tables (EWMA/CUSUM state,
-- per-joint baselines, confirmed-fault labels) are what should persist
-- indefinitely; they're computed FROM this table but don't need to live
-- IN it. Adjust this interval upward if a specific site's compliance or
-- audit requirements need longer raw retention - compression already
-- keeps the storage cost of holding it longer fairly cheap.
SELECT add_retention_policy('raw_telemetry', INTERVAL '180 days', if_not_exists => TRUE);