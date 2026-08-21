"""
Orchestrates the full Layer 4 pipeline for ONE joint's pending readings:
load persistent state -> stream EWMA/CUSUM updates over each new reading
-> write joint_features audit rows -> compute rolling slope -> load/score
anomaly model -> compute severity/critical-R/projected-days -> write
joint_risk_score -> advance watermark.

Same governing rule as app/physics_processor.py (requirement carried
forward from Layer 3): process_joint() never raises. Any failure
downgrades the written status ('feature_failed',
'insufficient_history_for_slope', 'baselining') rather than losing the
joint's watermark progress or crashing the poller
(app/feature_service.py).
"""
import logging
from datetime import datetime, timezone

from app.anomaly_model import load_model, score as anomaly_score
from app.breaker_risk_config import get_breaker_risk_config
from app.config import settings
from app.feature_baseline import record_feature_baseline_sample
from app.feature_math import (
    critical_r_for_breaker, cusum_step, ewma_step,
    projected_days_to_critical, rolling_slope_per_day, severity_band_from_residual,
)
from app.feature_state_store import (
    get_new_readings_for_joint, load_feature_state, save_feature_state,
)

logger = logging.getLogger(__name__)


async def process_joint(pool, panel_id: str, breaker_id: str) -> str:
    """Returns the status string written to joint_risk_score, for logging/metrics."""
    try:
        state = await load_feature_state(pool, panel_id, breaker_id)
        readings = await get_new_readings_for_joint(pool, panel_id, breaker_id, state.last_processed_ts)
        if not readings:
            return "no_new_readings"

        last_row = None
        for row in readings:
            # R_effective is recorded even during Layer 3's own
            # baseline_building status - track its EWMA from the
            # earliest possible point, more smoothing history is
            # strictly better once residual/CUSUM come online later.
            if row["r_effective_ohms"] is not None:
                state.ewma_r_effective = ewma_step(
                    state.ewma_r_effective, row["r_effective_ohms"], settings.ewma_span_readings
                )

            # residual_c (and therefore CUSUM against it) is only
            # available once Layer 3's status='ok' - i.e. its own
            # R_nominal baseline is already complete.
            if row["residual_c"] is not None:
                state.ewma_residual = ewma_step(
                    state.ewma_residual, row["residual_c"], settings.ewma_span_readings
                )

                baseline = await record_feature_baseline_sample(
                    pool, panel_id, breaker_id, row["residual_c"], settings.feature_baseline_min_readings
                )
                if baseline.baseline_complete:
                    state.cusum_value = cusum_step(
                        state.cusum_value, row["residual_c"],
                        baseline.baseline_mean, baseline.baseline_std, settings.cusum_k_slack,
                    )

                await _write_joint_feature_row(pool, row, state)

            state.last_processed_ts = row["ts"]
            last_row = row

        await save_feature_state(pool, state)

        return await _compute_and_write_risk_score(pool, panel_id, breaker_id, state, last_row)

    except Exception as exc:
        logger.exception("Unhandled error processing panel=%s breaker=%s - watermark NOT advanced, will retry next poll cycle", panel_id, breaker_id)
        await _safe_write_risk_row(
            pool, panel_id, breaker_id, ts=datetime.now(timezone.utc),
            anomaly_score=None, cusum_value=None, dr_dt=None, critical_r=None,
            projected_days=None, severity_band="unknown", status="feature_failed", error_detail=str(exc)[:2000],
        )
        return "feature_failed"


async def _write_joint_feature_row(pool, row, state) -> None:
    try:
        await pool.execute(
            """
            INSERT INTO joint_features (ts, panel_id, breaker_id, ewma_r_effective, ewma_residual, cusum_value, hour_of_day, i_rms)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NULL)
            ON CONFLICT (panel_id, breaker_id, ts) DO UPDATE SET
                ewma_r_effective = EXCLUDED.ewma_r_effective,
                ewma_residual = EXCLUDED.ewma_residual,
                cusum_value = EXCLUDED.cusum_value
            """,
            row["ts"], row["panel_id"], row["breaker_id"],
            state.ewma_r_effective, state.ewma_residual, state.cusum_value,
            row["ts"].hour,
        )
    except Exception:
        # Audit-trail write failing must never block the live risk
        # pipeline - joint_feature_state (the source of truth for
        # ongoing computation) is saved separately regardless of this.
        logger.exception("Failed to write joint_features audit row for panel=%s breaker=%s ts=%s", row["panel_id"], row["breaker_id"], row["ts"])


async def _compute_and_write_risk_score(pool, panel_id: str, breaker_id: str, state, last_row) -> str:
    now = datetime.now(timezone.utc)

    if state.ewma_residual is None:
        # Still in Layer 3's own baseline_building window, or this
        # joint's very first readings haven't produced a residual yet -
        # nothing meaningful to score.
        await _safe_write_risk_row(
            pool, panel_id, breaker_id, ts=now, anomaly_score=None, cusum_value=state.cusum_value,
            dr_dt=None, critical_r=None, projected_days=None, severity_band="baselining",
            status="baselining", error_detail=None,
        )
        return "baselining"

    slope_rows = await pool.fetch(
        """
        SELECT ts, ewma_r_effective FROM joint_features
        WHERE panel_id = $1 AND breaker_id = $2
          AND ts > now() - ($3 || ' days')::interval
          AND ewma_r_effective IS NOT NULL
        ORDER BY ts ASC
        """,
        panel_id, breaker_id, str(settings.slope_window_days),
    )

    dr_dt = None
    if len(slope_rows) >= settings.slope_min_readings:
        timestamps = [r["ts"].timestamp() for r in slope_rows]
        values = [r["ewma_r_effective"] for r in slope_rows]
        dr_dt = rolling_slope_per_day(timestamps, values)

    risk_cfg = get_breaker_risk_config(panel_id, breaker_id)
    # theta_thermal for this joint's critical-R calc: reuse the most
    # recent value Layer 3 computed for it (stored per-reading in
    # derived_metrics) rather than recomputing the CoolProp correlation
    # here - Layer 4 should never duplicate Layer 3's physics, only
    # consume its output.
    theta_row = await pool.fetchrow(
        """
        SELECT theta_thermal_k_per_w FROM derived_metrics
        WHERE panel_id = $1 AND breaker_id = $2 AND theta_thermal_k_per_w IS NOT NULL
        ORDER BY ts DESC LIMIT 1
        """,
        panel_id, breaker_id,
    )

    critical_r = projected_days = None
    if theta_row is not None and state.ewma_r_effective is not None:
        critical_r = critical_r_for_breaker(
            theta_row["theta_thermal_k_per_w"], risk_cfg.rated_current_amps, risk_cfg.critical_delta_t_c
        )
        projected_days = projected_days_to_critical(state.ewma_r_effective, critical_r, dr_dt)

    anomaly_score_val = None
    model = load_model(panel_id, breaker_id)
    if model is not None:
        anomaly_score_val = anomaly_score(model, {
            "ewma_r_effective": state.ewma_r_effective,
            "ewma_residual": state.ewma_residual,
            "cusum_value": state.cusum_value,
            "hour_of_day": last_row["ts"].hour if last_row is not None else None,
            "i_rms": None,  # not tracked in joint_feature_state; acceptable, anomaly_model.py imputes missing as 0.0
        })

    severity = severity_band_from_residual(state.ewma_residual)
    status = "scored" if len(slope_rows) >= settings.slope_min_readings else "insufficient_history_for_slope"

    await _safe_write_risk_row(
        pool, panel_id, breaker_id, ts=now, anomaly_score=anomaly_score_val,
        cusum_value=state.cusum_value, dr_dt=dr_dt, critical_r=critical_r,
        projected_days=projected_days, severity_band=severity, status=status, error_detail=None,
    )
    return status


async def _safe_write_risk_row(pool, panel_id, breaker_id, ts, anomaly_score, cusum_value, dr_dt, critical_r, projected_days, severity_band, status, error_detail) -> None:
    try:
        await pool.execute(
            """
            INSERT INTO joint_risk_score
                (ts, panel_id, breaker_id, anomaly_score, cusum_value, dr_dt_ohms_per_day,
                 critical_r_ohms, projected_days_to_critical, severity_band, status, error_detail)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            ts, panel_id, breaker_id, anomaly_score, cusum_value, dr_dt,
            critical_r, projected_days, severity_band, status, error_detail,
        )
    except Exception:
        logger.exception(
            "Failed to write joint_risk_score row for panel=%s breaker=%s - this cycle's risk "
            "assessment is lost, but joint_feature_state's watermark was already advanced, so "
            "the underlying readings won't be reprocessed either; next cycle's fresh computation "
            "will simply supersede this gap",
            panel_id, breaker_id,
        )
