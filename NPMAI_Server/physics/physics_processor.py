"""
Orchestrates the full Layer 3 pipeline for ONE raw reading: dedup check
-> theta_thermal/R_effective (CoolProp) -> baseline bookkeeping ->
T_expected (TESPy) -> residual -> PyPSA cross-check (best-effort) ->
write derived_metrics.

The governing rule (requirement #7): process_reading() never raises. A
failure anywhere downgrades what gets recorded (fewer fields, a
'physics_failed'/'baseline_building'/'low_current_skip' status) - it
never loses the reading and never propagates an exception up to the
Redis consumer loop (app/physics_service.py), the same "log it and move
on" philosophy already used in app/mqtt_client.py's _on_message.
"""
import logging

from app.baseline import record_baseline_sample
from app.breaker_thermal_config import get_breaker_thermal_config
from app.config import settings
from app.derived_metrics_writer import DerivedMetricsRow, derived_metrics_exists, upsert_derived_metrics
from app.physics.pypsa_crosscheck import CrossCheckError, cross_check_loss_ratio
from app.physics.tespy_model import PhysicsSolveError, compute_expected_t_tespy
from app.physics.thermal_model import compute_r_effective, compute_theta_thermal

logger = logging.getLogger(__name__)


async def process_reading(pool, reading: dict) -> str:
    """
    `reading` is the dict decoded from the Redis Stream event Layer 2
    publishes (see app/redis_events.py's publish_batch) - same shape as
    TelemetryPayload: device_id, panel_id, breaker_id, timestamp (unix
    epoch seconds - matches raw_telemetry.ts via to_timestamp(), see
    app/derived_metrics_writer.py), I_rms, T_hotspot, T_ambient, wifi_rssi.

    Returns the status string that was written to derived_metrics, for
    logging/metrics purposes.
    """
    panel_id = reading["panel_id"]
    breaker_id = reading["breaker_id"]
    device_id = reading["device_id"]
    ts_epoch = reading["timestamp"]
    i_rms = float(reading["I_rms"])
    t_hotspot = float(reading["T_hotspot"])
    t_ambient = float(reading["T_ambient"])

    try:
        if await derived_metrics_exists(pool, panel_id, breaker_id, ts_epoch):
            logger.debug(
                "Duplicate delivery for panel=%s breaker=%s ts=%s - already processed, skipping",
                panel_id, breaker_id, ts_epoch,
            )
            return "duplicate_skip"
    except Exception:
        logger.exception(
            "Dedup check failed for panel=%s breaker=%s ts=%s - proceeding anyway; worst case is a "
            "harmless re-computation, the ON CONFLICT upsert below still protects derived_metrics itself",
            panel_id, breaker_id, ts_epoch,
        )

    if i_rms < settings.min_current_for_physics_amps:
        row = DerivedMetricsRow(
            ts_epoch=ts_epoch, device_id=device_id, panel_id=panel_id, breaker_id=breaker_id,
            r_effective_ohms=None, t_expected_c=None, residual_c=None,
            theta_thermal_k_per_w=None, r_nominal_ohms=None, pypsa_cross_check_ratio=None,
            status="low_current_skip", error_detail=None,
        )
        await _safe_write(pool, row)
        return row.status

    thermal_cfg = get_breaker_thermal_config(panel_id, breaker_id)

    theta_thermal = None
    r_effective = None
    try:
        if thermal_cfg.theta_thermal_override_k_per_w is not None:
            theta_thermal = thermal_cfg.theta_thermal_override_k_per_w
        else:
            theta_thermal = compute_theta_thermal(
                t_ambient_c=t_ambient, t_hotspot_c=t_hotspot,
                area_m2=thermal_cfg.area_m2, char_length_m=thermal_cfg.char_length_m,
                emissivity=thermal_cfg.emissivity,
            )
        r_effective = compute_r_effective(i_rms, t_hotspot, t_ambient, theta_thermal)
    except Exception as exc:
        logger.exception(
            "theta_thermal/R_effective calc (CoolProp) failed for panel=%s breaker=%s ts=%s",
            panel_id, breaker_id, ts_epoch,
        )
        row = DerivedMetricsRow(
            ts_epoch=ts_epoch, device_id=device_id, panel_id=panel_id, breaker_id=breaker_id,
            r_effective_ohms=None, t_expected_c=None, residual_c=None,
            theta_thermal_k_per_w=None, r_nominal_ohms=None, pypsa_cross_check_ratio=None,
            status="physics_failed", error_detail=str(exc)[:2000],
        )
        await _safe_write(pool, row)
        return row.status

    try:
        baseline = await record_baseline_sample(
            pool, panel_id, breaker_id, r_effective, settings.baseline_min_readings
        )
    except Exception:
        logger.exception(
            "Baseline bookkeeping failed for panel=%s breaker=%s ts=%s - recording R_effective only; "
            "we never compute a residual off of baseline state we're not sure was updated correctly",
            panel_id, breaker_id, ts_epoch,
        )
        row = DerivedMetricsRow(
            ts_epoch=ts_epoch, device_id=device_id, panel_id=panel_id, breaker_id=breaker_id,
            r_effective_ohms=r_effective, t_expected_c=None, residual_c=None,
            theta_thermal_k_per_w=theta_thermal, r_nominal_ohms=None, pypsa_cross_check_ratio=None,
            status="physics_failed", error_detail="baseline bookkeeping error",
        )
        await _safe_write(pool, row)
        return row.status

    if not baseline.baseline_complete:
        row = DerivedMetricsRow(
            ts_epoch=ts_epoch, device_id=device_id, panel_id=panel_id, breaker_id=breaker_id,
            r_effective_ohms=r_effective, t_expected_c=None, residual_c=None,
            theta_thermal_k_per_w=theta_thermal, r_nominal_ohms=None, pypsa_cross_check_ratio=None,
            status="baseline_building", error_detail=None,
        )
        await _safe_write(pool, row)
        return row.status

    t_expected = None
    status = "ok"
    error_detail = None
    try:
        t_expected = compute_expected_t_tespy(i_rms, baseline.r_nominal_ohms, theta_thermal, t_ambient)
    except PhysicsSolveError as exc:
        logger.warning(
            "TESPy expected-T solve failed for panel=%s breaker=%s ts=%s: %s - R_effective is still "
            "recorded (independent closed-form calc), only T_expected/residual are unavailable",
            panel_id, breaker_id, ts_epoch, exc,
        )
        status = "physics_failed"
        error_detail = str(exc)[:2000]
    except Exception as exc:
        logger.exception(
            "Unexpected error in TESPy expected-T solve for panel=%s breaker=%s ts=%s",
            panel_id, breaker_id, ts_epoch,
        )
        status = "physics_failed"
        error_detail = str(exc)[:2000]

    residual = (t_hotspot - t_expected) if t_expected is not None else None

    pypsa_ratio = None
    try:
        pypsa_ratio = cross_check_loss_ratio(i_rms, r_effective, v_nom_kv=settings.panel_v_nom_kv)
    except CrossCheckError as exc:
        logger.warning(
            "PyPSA cross-check did not converge for panel=%s breaker=%s ts=%s: %s - non-fatal, "
            "this is a QA signal only and does not affect status",
            panel_id, breaker_id, ts_epoch, exc,
        )
    except Exception:
        logger.exception(
            "Unexpected error in PyPSA cross-check for panel=%s breaker=%s ts=%s",
            panel_id, breaker_id, ts_epoch,
        )

    row = DerivedMetricsRow(
        ts_epoch=ts_epoch, device_id=device_id, panel_id=panel_id, breaker_id=breaker_id,
        r_effective_ohms=r_effective, t_expected_c=t_expected, residual_c=residual,
        theta_thermal_k_per_w=theta_thermal, r_nominal_ohms=baseline.r_nominal_ohms,
        pypsa_cross_check_ratio=pypsa_ratio, status=status, error_detail=error_detail,
    )
    await _safe_write(pool, row)
    return status


async def _safe_write(pool, row: DerivedMetricsRow) -> None:
    try:
        await upsert_derived_metrics(pool, row)
    except Exception:
        logger.exception(
            "Failed to write derived_metrics row for panel=%s breaker=%s ts=%s - this reading's physics "
            "results are lost, but the raw reading itself is safe in raw_telemetry and can be "
            "reprocessed/backfilled later",
            row.panel_id, row.breaker_id, row.ts_epoch,
        )