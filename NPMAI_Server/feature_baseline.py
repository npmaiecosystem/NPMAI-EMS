"""
Per-joint RESIDUAL baseline tracking - the Layer 4 counterpart to
app/baseline.py's R_nominal tracking in Layer 3. Structurally near-
identical to that file on purpose (same running-sum math, same
row-locked upsert pattern, same "why not just fabricate a fleet-wide
default" reasoning) - see that file for the fuller explanation, this
docstring only covers what's DIFFERENT here.

What's different: this baseline only starts accumulating once Layer 3
is already producing status='ok' rows (i.e. residual_c is non-NULL) for
a joint - Layer 3's own baseline (R_nominal) must complete FIRST. So a
brand-new joint goes through two sequential baselining windows before
CUSUM-based risk scoring is meaningful: Layer 3's (learning R_nominal,
~settings.baseline_min_readings valid readings) then Layer 4's (learning
this joint's own residual noise floor, ~settings.feature_baseline_min_readings
readings). This is intentional, not redundant - see migrations/
005_layer4_features_and_risk.sql's comment on joint_feature_baseline
for why these are deliberately two separate baselines answering two
separate questions.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeatureBaselineState:
    reading_count: int
    baseline_mean: Optional[float]
    baseline_std: Optional[float]
    baseline_complete: bool


def compute_updated_stats(count: int, r_sum: float, r_sumsq: float, new_value: float):
    """Identical running-sum approach to app/baseline.py's version - see
    that file for why plain sum/sum-of-squares is sufficient here
    (moderate sample counts, not a numerically-fussy streaming-variance
    use case)."""
    return count + 1, r_sum + new_value, r_sumsq + new_value * new_value


def mean_std(count: int, r_sum: float, r_sumsq: float):
    if count < 1:
        return None, None
    mean = r_sum / count
    variance = max(r_sumsq / count - mean * mean, 0.0)
    return mean, variance ** 0.5


async def record_feature_baseline_sample(
    pool: asyncpg.Pool,
    panel_id: str,
    breaker_id: str,
    residual_c: float,
    baseline_min_readings: int,
) -> FeatureBaselineState:
    """
    Same row-locked upsert pattern as app/baseline.py's
    record_baseline_sample() - see that function's docstring for the
    multi-worker-safety rationale, which applies identically here.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO joint_feature_baseline
                    (panel_id, breaker_id, reading_count, residual_sum, residual_sumsq, updated_at)
                VALUES ($1, $2, 0, 0, 0, now())
                ON CONFLICT (panel_id, breaker_id) DO NOTHING
                """,
                panel_id, breaker_id,
            )

            row = await conn.fetchrow(
                """
                SELECT reading_count, residual_sum, residual_sumsq, baseline_mean, baseline_std, baseline_complete
                FROM joint_feature_baseline
                WHERE panel_id = $1 AND breaker_id = $2
                FOR UPDATE
                """,
                panel_id, breaker_id,
            )

            if row["baseline_complete"]:
                # Same reasoning as Layer 3: locked in once complete, so
                # a real slow drift in residual noise character doesn't
                # just get silently absorbed as "the new normal" forever.
                return FeatureBaselineState(
                    reading_count=row["reading_count"],
                    baseline_mean=row["baseline_mean"],
                    baseline_std=row["baseline_std"],
                    baseline_complete=True,
                )

            new_count, new_sum, new_sumsq = compute_updated_stats(
                row["reading_count"], row["residual_sum"], row["residual_sumsq"], residual_c
            )
            baseline_complete = new_count >= baseline_min_readings
            baseline_mean = baseline_std = None
            if baseline_complete:
                baseline_mean, baseline_std = mean_std(new_count, new_sum, new_sumsq)
                logger.info(
                    "Residual baseline complete for panel=%s breaker=%s after %d readings: "
                    "mean=%.4f std=%.4f",
                    panel_id, breaker_id, new_count, baseline_mean, baseline_std,
                )

            await conn.execute(
                """
                UPDATE joint_feature_baseline
                SET reading_count = $3, residual_sum = $4, residual_sumsq = $5,
                    baseline_mean = $6, baseline_std = $7, baseline_complete = $8, updated_at = now()
                WHERE panel_id = $1 AND breaker_id = $2
                """,
                panel_id, breaker_id, new_count, new_sum, new_sumsq,
                baseline_mean, baseline_std, baseline_complete,
            )

            return FeatureBaselineState(
                reading_count=new_count,
                baseline_mean=baseline_mean,
                baseline_std=baseline_std,
                baseline_complete=baseline_complete,
            )
