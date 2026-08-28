import math

from pydantic import BaseModel, Field, field_validator


class TelemetryPayload(BaseModel):
    """
    The one schema both ingestion paths (MQTT subscriber and the HTTPS
    /ingest/telemetry fallback) validate against, matching the JSON shape
    the Layer 1 ESP32 firmware emits exactly:
    {device_id, panel_id, breaker_id, timestamp, I_rms, T_hotspot,
     T_ambient, wifi_rssi}.

    Bounds below are deliberately generous physical sanity checks, not
    tight operational thresholds - that judgment belongs to Layer 3/4's
    physics-aware trend detection, not to schema validation. This layer's
    only job is rejecting garbage (wrong types, impossible values,
    NaN/Infinity, missing fields), never plausible-but-unusual readings.
    """

    device_id: str = Field(..., min_length=1, max_length=24)
    panel_id: str = Field(..., min_length=1, max_length=24)
    breaker_id: str = Field(..., min_length=1, max_length=24)
    # Unix epoch seconds. 0 is explicitly allowed - it's the Layer 1
    # firmware's documented sentinel for "device clock not yet
    # NTP-synced" (see TimeSync.h in the edge firmware), not an error.
    timestamp: int = Field(..., ge=0)
    I_rms: float = Field(..., ge=0, le=100_000, description="Amps, primary-side RMS current")
    T_hotspot: float = Field(..., ge=-50, le=500, description="deg C")
    T_ambient: float = Field(..., ge=-50, le=500, description="deg C")
    wifi_rssi: int = Field(..., ge=-120, le=0, description="dBm; 0 = device reported not connected")

    @field_validator("I_rms", "T_hotspot", "T_ambient")
    @classmethod
    def must_be_finite(cls, v: float) -> float:
        # Standard JSON has no NaN/Infinity literals, but Python's json
        # module accepts the non-standard tokens NaN/Infinity/-Infinity
        # by default, so a buggy or malicious client could still get one
        # past basic JSON parsing. Reject explicitly rather than letting
        # a NaN silently corrupt a downstream EWMA/CUSUM calculation.
        if not math.isfinite(v):
            raise ValueError("value must be finite (no NaN/Infinity)")
        return v