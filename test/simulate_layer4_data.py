"""
Generates synthetic derived_metrics rows directly into the DB, for
testing the Layer 4 pipeline (feature_processor.py / feature_service.py)
end to end without waiting on Layer 3 or real hardware. Reuses the exact
simulation validated in app/feature_math.py's own test suite (one
healthy joint, one slowly-degrading joint) so results here should match
those already-verified numbers (CUSUM≈0 for healthy, ≈6200 for the
degrading joint over 45 days, dR/dt matching the injected drift rate).

Usage:
    python -m tests.simulate_layer4_data --panel panel-2F-main \\
        --healthy-breaker brk-01 --degrading-breaker brk-02

This INSERTs directly into raw_telemetry AND derived_metrics (bypassing
Layer 1/2/3 entirely) since the goal here is testing Layer 4 in
isolation - if you want a true end-to-end test including Layers 1-3,
use tests/simulate_device.py (Layer 2's MQTT/HTTPS simulator) instead
and let the real pipeline compute derived_metrics.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg
import numpy as np

DATABASE_URL_DEFAULT = "postgresql://npmai:npmai_dev_password@localhost:5432/npmai"


def simulate_joint(hours: int, r_start: float, drift_per_hour: float, theta_thermal: float, seed: int):
    rng = np.random.default_rng(seed)
    R = r_start + drift_per_hour * np.arange(hours)
    I = 40 + 20 * np.sin(np.arange(hours) / 24 * 2 * np.pi) + rng.normal(0, 2, hours)
    I = np.clip(I, 5, None)
    T_ambient = 27 + 3 * np.sin(np.arange(hours) / 24 * 2 * np.pi) + rng.normal(0, 0.3, hours)
    # residual, matching Layer 3's actual definition: measured minus
    # expected-at-baseline-R - i.e. zero when R hasn't drifted from r_start
    residual = (I ** 2 * theta_thermal * (R - r_start)) + rng.normal(0, 0.3, hours)
    t_hotspot = T_ambient + (I ** 2 * theta_thermal * R)  # actual measured temp, for realism/completeness
    return R, I, T_ambient, t_hotspot, residual


async def insert_joint(conn, device_id, panel_id, breaker_id, hours, R, I, T_ambient, t_hotspot, residual, theta_thermal, start_time):
    for i in range(hours):
        ts = start_time + timedelta(hours=i)
        status = "ok" if i >= 168 else "baseline_building"  # first 7 days = Layer 3's own baselining window
        r_nominal = R[0] if i >= 168 else None
        residual_val = float(residual[i]) if status == "ok" else None

        await conn.execute(
            """
            INSERT INTO raw_telemetry (ts, device_id, panel_id, breaker_id, i_rms, t_hotspot, t_ambient, wifi_rssi)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (panel_id, breaker_id, ts) DO NOTHING
            """,
            ts, device_id, panel_id, breaker_id, float(I[i]), float(t_hotspot[i]), float(T_ambient[i]), -55,
        )
        await conn.execute(
            """
            INSERT INTO derived_metrics
                (ts, device_id, panel_id, breaker_id, r_effective_ohms, theta_thermal_k_per_w,
                 r_nominal_ohms, t_expected_c, residual_c, pypsa_cross_check_ratio, status, error_detail)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NULL, $10, NULL)
            ON CONFLICT (panel_id, breaker_id, ts) DO NOTHING
            """,
            ts, device_id, panel_id, breaker_id, float(R[i]), theta_thermal,
            r_nominal, (float(t_hotspot[i]) - residual_val) if residual_val is not None else None,
            residual_val, status,
        )


async def main_async(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        hours = args.days * 24
        start_time = datetime.now(timezone.utc) - timedelta(hours=hours)
        theta_thermal = 59.2  # matches the value already validated against real CoolProp/TESPy calls in Layer 3 testing

        print(f"Generating {hours} hourly readings ({args.days} days) for two joints...")

        R_h, I_h, Ta_h, Th_h, resid_h = simulate_joint(hours, r_start=0.0003, drift_per_hour=0.0, theta_thermal=theta_thermal, seed=1)
        await insert_joint(conn, f"esp32-sim-{args.healthy_breaker}", args.panel, args.healthy_breaker,
                            hours, R_h, I_h, Ta_h, Th_h, resid_h, theta_thermal, start_time)
        print(f"  {args.healthy_breaker}: healthy, flat R={0.0003} ohm throughout")

        R_f, I_f, Ta_f, Th_f, resid_f = simulate_joint(hours, r_start=0.0003, drift_per_hour=0.0000006, theta_thermal=theta_thermal, seed=2)
        await insert_joint(conn, f"esp32-sim-{args.degrading_breaker}", args.panel, args.degrading_breaker,
                            hours, R_f, I_f, Ta_f, Th_f, resid_f, theta_thermal, start_time)
        print(f"  {args.degrading_breaker}: degrading, R drifts {0.0003} -> {R_f[-1]:.6f} ohm over {args.days} days")

        print("\nDone. Run the Layer 4 feature_service against this DB and check joint_risk_score - "
              f"expect '{args.healthy_breaker}' to stay severity_band='healthy' throughout, and "
              f"'{args.degrading_breaker}' to climb through minor/intermediate/serious as CUSUM accumulates.")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=DATABASE_URL_DEFAULT)
    parser.add_argument("--panel", default="panel-2F-main")
    parser.add_argument("--healthy-breaker", default="brk-sim-healthy")
    parser.add_argument("--degrading-breaker", default="brk-sim-degrading")
    parser.add_argument("--days", type=int, default=45)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
