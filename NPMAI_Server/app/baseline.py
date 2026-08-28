"""
Per-joint baseline tracking (requirement #5): a brand-new breaker has no
prior R_effective history, so there's no "expected healthy resistance"
to compare against yet - T_expected/residual literally cannot be
computed for it. Rather than fabricate a fleet-wide default (too strict
for a naturally-higher-resistance-but-fine joint, too lenient for one
that's already marginal at install time), we learn each joint's own
R_nominal from its own first `baseline_min_readings` valid readings, on
the assumption a freshly-installed/inspected joint starts healthy.

Readings during this window get status='baseline_building' in
derived_metrics: R_effective is still recorded (so the baseline data
itself is visible/auditable), but T_expected/residual are NULL - there
is nothing yet to compare against, and inventing a residual against an
incomplete baseline would be worse than admitting we don't have one.

State lives in Postgres (joint_baseline table, see
migrations/002_derived_metrics.sql), not Redis/in-memory, so a service
restart doesn't lose a joint's partial baseline progress and reset its
learning window.

Known limitation (documented, not solved here): if a joint is physically
repaired/rewired, its R_nominal should reset and re-learn from scratch,
but this service has no signal that a repair happened. For the pilot,
that's a manual operation: `DELETE FROM joint_baseline WHERE panel_id=...
AND breaker_id=...` after a confirmed fix, so the next reading restarts
baseline-building. Layer 5/6 (maintenance ticket integration) would be
the natural place to automate this later.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BaselineState:
    reading_count: int
    r_nominal_ohms: Optional[float]
    baseline_complete: bool


def compute_updated_stats(count: int, r_sum: float, r_sumsq: float, new_r: float):
    """Pure running-sum update (no DB) - we only need the final mean/std,
    not numerically-fussy online variance, so plain sum/sum-of-squares is
    enough at this sample count. Exposed standalone so
    tests/test_physics.py can exercise the math without a database."""
    return count + 1, r_sum + new_r, r_sumsq + new_r * new_r


def mean_std(count: int, r_sum: float, r_sumsq: float):
    """Returns (mean, std), or (None, None) if count == 0."""
    if count < 1:
        return None, None
    mean = r_sum / count
    variance = max(r_sumsq / count - mean * mean, 0.0)  # clamp for float rounding
    return mean, variance ** 0.5


async def record_baseline_sample(
    pool: asyncpg.Pool,
    panel_id: str,
    breaker_id: str,
    r_effective_ohms: float,
    baseline_min_readings: int,
) -> BaselineState:
    """
    Upserts one new R_effective sample into this joint's baseline
    accumulator and returns the resulting state. Row-locked
    (SELECT ... FOR UPDATE) so this stays correct if Layer 3 is ever
    scaled to multiple consumer processes in the same Redis consumer
    group (see app/physics_service.py) and two workers happen to process
    readings for the same breaker close together.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO joint_baseline (panel_id, breaker_id, reading_count, r_sum, r_sumsq, updated_at)
                VALUES ($1, $2, 0, 0, 0, now())
                ON CONFLICT (panel_id, breaker_id) DO NOTHING
                """,
                panel_id, breaker_id,
            )

            row = await conn.fetchrow(
                """
                SELECT reading_count, r_sum, r_sumsq, r_nominal_ohms, baseline_complete
                FROM joint_baseline
                WHERE panel_id = $1 AND breaker_id = $2
                FOR UPDATE
                """,
                panel_id, breaker_id,
            )

            if row["baseline_complete"]:
                # Already locked in - don't keep drifting it with new
                # samples forever, or a slow real resistance increase
                # would just get absorbed into "the new normal" instead
                # of showing up as a residual. Nothing to update.
                return BaselineState(
                    reading_count=row["reading_count"],
                    r_nominal_ohms=row["r_nominal_ohms"],
                    baseline_complete=True,
                )

            new_count, new_sum, new_sumsq = compute_updated_stats(
                row["reading_count"], row["r_sum"], row["r_sumsq"], r_effective_ohms
            )
            baseline_complete = new_count >= baseline_min_readings
            r_nominal_ohms = None
            if baseline_complete:
                r_nominal_ohms, _std = mean_std(new_count, new_sum, new_sumsq)
                logger.info(
                    "Baseline complete for panel=%s breaker=%s after %d readings: r_nominal=%.6f ohms",
                    panel_id, breaker_id, new_count, r_nominal_ohms,
                )

            await conn.execute(
                """
                UPDATE joint_baseline
                SET reading_count = $3, r_sum = $4, r_sumsq = $5,
                    r_nominal_ohms = $6, baseline_complete = $7, updated_at = now()
                WHERE panel_id = $1 AND breaker_id = $2
                """,
                panel_id, breaker_id, new_count, new_sum, new_sumsq, r_nominal_ohms, baseline_complete,
            )

            return BaselineState(
                reading_count=new_count,
                r_nominal_ohms=r_nominal_ohms,
                baseline_complete=baseline_complete,
            )