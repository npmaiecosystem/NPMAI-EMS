"""
Writes one row per processed reading to derived_metrics. Two entry points:

- derived_metrics_exists(): a cheap pre-check the processor uses to make
  reprocessing an at-least-once-delivered Redis Stream message a safe
  no-op (see app/physics_processor.py). This matters more here than it
  did for Layer 2's raw insert, because baseline bookkeeping
  (app/baseline.py's running sums) is NOT naturally idempotent the way
  an INSERT ... ON CONFLICT DO NOTHING is - redelivering the same
  reading into the baseline accumulator would double-count it.
- upsert_derived_metrics(): the actual write, itself also
  ON CONFLICT-safe as defense in depth.
"""
from dataclasses import dataclass
from typing import Optional

import asyncpg


@dataclass
class DerivedMetricsRow:
    ts_epoch: int
    device_id: str
    panel_id: str
    breaker_id: str
    r_effective_ohms: Optional[float]
    t_expected_c: Optional[float]
    residual_c: Optional[float]
    theta_thermal_k_per_w: Optional[float]
    r_nominal_ohms: Optional[float]
    pypsa_cross_check_ratio: Optional[float]
    status: str
    error_detail: Optional[str]


async def derived_metrics_exists(pool: asyncpg.Pool, panel_id: str, breaker_id: str, ts_epoch: int) -> bool:
    row = await pool.fetchrow(
        """
        SELECT 1 FROM derived_metrics
        WHERE panel_id = $1 AND breaker_id = $2 AND ts = to_timestamp($3)
        """,
        panel_id, breaker_id, ts_epoch,
    )
    return row is not None


async def upsert_derived_metrics(pool: asyncpg.Pool, row: DerivedMetricsRow) -> None:
    await pool.execute(
        """
        INSERT INTO derived_metrics
            (ts, device_id, panel_id, breaker_id, r_effective_ohms, theta_thermal_k_per_w,
             r_nominal_ohms, t_expected_c, residual_c, pypsa_cross_check_ratio, status, error_detail)
        VALUES
            (to_timestamp($1), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (panel_id, breaker_id, ts) DO UPDATE SET
            r_effective_ohms = EXCLUDED.r_effective_ohms,
            theta_thermal_k_per_w = EXCLUDED.theta_thermal_k_per_w,
            r_nominal_ohms = EXCLUDED.r_nominal_ohms,
            t_expected_c = EXCLUDED.t_expected_c,
            residual_c = EXCLUDED.residual_c,
            pypsa_cross_check_ratio = EXCLUDED.pypsa_cross_check_ratio,
            status = EXCLUDED.status,
            error_detail = EXCLUDED.error_detail,
            computed_at = now()
        """,
        row.ts_epoch, row.device_id, row.panel_id, row.breaker_id,
        row.r_effective_ohms, row.theta_thermal_k_per_w, row.r_nominal_ohms,
        row.t_expected_c, row.residual_c, row.pypsa_cross_check_ratio,
        row.status, row.error_detail,
    )