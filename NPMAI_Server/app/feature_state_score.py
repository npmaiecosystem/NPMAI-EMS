"""
Loads/saves each joint's persistent joint_feature_state row - the EWMA
and CUSUM running values, plus last_processed_ts (this joint's watermark
- see app/feature_service.py for how the poller uses it to find only
NEW derived_metrics rows each cycle). This is what turns
feature_math.py's stateless ewma_step()/cusum_step() into an actual
streaming computation across service restarts: the state lives in
Postgres, not in a Python variable, so a restart resumes exactly where
it left off instead of re-seeding EWMA from scratch (the same class of
bug found and flagged in the Layer 1 CurrentSensor.cpp review - fixed
here by construction, since there's no in-memory-only path at all).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import asyncpg


@dataclass
class JointFeatureState:
    panel_id: str
    breaker_id: str
    ewma_r_effective: Optional[float]
    ewma_residual: Optional[float]
    cusum_value: float
    last_processed_ts: Optional[datetime]


async def load_feature_state(pool: asyncpg.Pool, panel_id: str, breaker_id: str) -> JointFeatureState:
    """
    Always returns a usable state object, creating a fresh all-NULL row
    if this is the joint's first-ever poll cycle - callers never need to
    special-case "no state yet" separately from "state exists but
    EWMA/CUSUM haven't been seeded" (ewma_step's own None-handling covers
    the latter).
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO joint_feature_state (panel_id, breaker_id, cusum_value, updated_at)
            VALUES ($1, $2, 0, now())
            ON CONFLICT (panel_id, breaker_id) DO NOTHING
            """,
            panel_id, breaker_id,
        )
        row = await conn.fetchrow(
            """
            SELECT panel_id, breaker_id, ewma_r_effective, ewma_residual, cusum_value, last_processed_ts
            FROM joint_feature_state
            WHERE panel_id = $1 AND breaker_id = $2
            """,
            panel_id, breaker_id,
        )
        return JointFeatureState(
            panel_id=row["panel_id"], breaker_id=row["breaker_id"],
            ewma_r_effective=row["ewma_r_effective"], ewma_residual=row["ewma_residual"],
            cusum_value=row["cusum_value"], last_processed_ts=row["last_processed_ts"],
        )


async def save_feature_state(pool: asyncpg.Pool, state: JointFeatureState) -> None:
    await pool.execute(
        """
        UPDATE joint_feature_state
        SET ewma_r_effective = $3, ewma_residual = $4, cusum_value = $5,
            last_processed_ts = $6, updated_at = now()
        WHERE panel_id = $1 AND breaker_id = $2
        """,
        state.panel_id, state.breaker_id,
        state.ewma_r_effective, state.ewma_residual, state.cusum_value, state.last_processed_ts,
    )


async def get_joints_with_pending_readings(pool: asyncpg.Pool) -> list[tuple[str, str]]:
    """
    Returns every (panel_id, breaker_id) that has at least one
    derived_metrics row newer than that joint's own watermark - i.e.
    what feature_service.py's poller needs to process this cycle. A
    joint with no joint_feature_state row yet (first time ever seen) is
    included via the LEFT JOIN + COALESCE to the epoch, so brand-new
    joints get picked up on their very first poll cycle without any
    separate bootstrap step.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT dm.panel_id, dm.breaker_id
        FROM derived_metrics dm
        LEFT JOIN joint_feature_state jfs
            ON jfs.panel_id = dm.panel_id AND jfs.breaker_id = dm.breaker_id
        WHERE dm.status IN ('ok', 'baseline_building')
          AND dm.ts > COALESCE(jfs.last_processed_ts, to_timestamp(0))
        """
    )
    return [(r["panel_id"], r["breaker_id"]) for r in rows]


async def get_new_readings_for_joint(
    pool: asyncpg.Pool, panel_id: str, breaker_id: str, since_ts: Optional[datetime], limit: int = 5000,
) -> list[asyncpg.Record]:
    """
    Ordered oldest-first (ASC) - EWMA/CUSUM are sequential, order-
    dependent computations, so this must never be fed out of order.
    limit is a safety cap, not an expected normal case: at this pilot's
    scale a joint should never accumulate anywhere near 5000 unprocessed
    readings between poll cycles (default 60s) unless this service was
    down for a very long time - if that limit is ever actually hit,
    the next poll cycle simply picks up where this one's watermark left
    off, so no data is lost, it just takes multiple cycles to catch up.
    """
    since = since_ts or datetime.fromtimestamp(0)
    return await pool.fetch(
        """
        SELECT ts, panel_id, breaker_id, r_effective_ohms, residual_c, status
        FROM derived_metrics
        WHERE panel_id = $1 AND breaker_id = $2 AND ts > $3
          AND status IN ('ok', 'baseline_building')
        ORDER BY ts ASC
        LIMIT $4
        """,
        panel_id, breaker_id, since, limit,
    )
