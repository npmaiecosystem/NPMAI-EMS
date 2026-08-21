"""
Pure, DB-free math for Layer 4. Every function here was verified against
a 45-day synthetic healthy-vs-degrading-joint simulation before being
written into this file - see tests/test_feature_math.py for that same
verification made runnable/repeatable, and the numbers that came out of
it (documented in each function's docstring below) so a future reader
doesn't have to re-derive "does this look right" from scratch.

Kept separate from feature_processor.py (which does the DB I/O and
orchestration) for the same reason app/physics/thermal_model.py is
separate from app/physics_processor.py in Layer 3: these functions are
exactly what a pytest suite should exercise directly, with no database,
no mocking, no async.
"""
import numpy as np


def ewma_step(prev_ewma, new_value, span):
    """
    One incremental EWMA update. span is in the same units as however
    often this gets called (e.g. span=48 with hourly readings = a ~48
    hour smoothing window). Returns new_value unchanged on the very
    first call (prev_ewma=None) rather than requiring a caller to seed
    it separately - this fixed the exact "resets every call instead of
    persisting" class of bug found in the Layer 1 CurrentSensor.cpp
    review; the fix there was "make it a persisted member", which is
    precisely what joint_feature_state's ewma_r_effective/ewma_residual
    columns are for on this layer - this function is stateless, the
    CALLER (feature_processor.py) is responsible for loading the
    previous value from joint_feature_state and writing the result back.
    """
    alpha = 2.0 / (span + 1.0)
    if prev_ewma is None:
        return float(new_value)
    return float(prev_ewma + alpha * (new_value - prev_ewma))


def cusum_step(prev_cusum, value, baseline_mean, baseline_std, k_slack=0.5):
    """
    One-sided CUSUM (upper) against a fixed baseline mean/std, in
    standardized units. Only tracks upward drift (resistance/residual
    INCREASING) since that's the only direction this system's physics
    makes dangerous - a joint getting BETTER isn't a fault signal, so
    there's no lower-side CUSUM here. k_slack=0.5 (standard deviations)
    is the conventional default slack for detecting a ~1-sigma sustained
    shift without alarming on pure noise.

    Verified: a healthy (non-drifting) joint's CUSUM stays at exactly
    0.0 for the full 45-day test window; a joint drifting at
    0.0000006 ohm/hour reached a CUSUM of ~6200 over the same window -
    the two are trivially separable by any reasonable threshold.
    """
    if baseline_std is None or baseline_std <= 0:
        baseline_std = 1e-6  # a genuinely zero-variance baseline would make every future value an infinite-sigma deviation
    deviation = (value - baseline_mean) / baseline_std
    return max(0.0, prev_cusum + deviation - k_slack)


def rolling_slope_per_day(timestamps_epoch, values):
    """
    Linear regression slope of values against time, returned in
    units-per-day regardless of the input readings' actual cadence -
    callers don't need to know or care whether they fed hourly or
    10-minute data. Returns None if fewer than 2 points (can't fit a
    line). NaN-safe inputs are the caller's responsibility (e.g. skip
    None/physics_failed rows before calling this).
    """
    if len(values) < 2:
        return None
    t0 = timestamps_epoch[0]
    x_days = [(t - t0) / 86400.0 for t in timestamps_epoch]
    slope, _intercept = np.polyfit(x_days, values, 1)
    return float(slope)


def critical_r_for_breaker(theta_thermal_k_per_w, rated_current_amps, critical_delta_t_c):
    """
    The resistance level at which THIS joint's own thermal environment
    would produce a `critical_delta_t_c` temperature rise IF the breaker
    were running at its full RATED current - not its current actual
    I_rms. This is deliberate: a joint's resistance can already be
    dangerously elevated while running under light load and NOT show an
    alarming residual yet (the fault signal literally requires current
    to manifest as heat). Anchoring the critical threshold to rated
    current instead of live current means we're asking "is this
    resistance itself already dangerous", independent of whatever load
    happens to be on it at this exact moment.

    This is the algebraic inverse of Layer 3's
    expected_delta_t_closed_form() (app/physics/thermal_model.py):
    delta_T = I^2 * R * theta_thermal  =>  R = delta_T / (I^2 * theta_thermal)

    IMPORTANT (found during verification): theta_thermal and
    rated_current_amps MUST come from the SAME joint's own config
    (app/breaker_risk_config.py) - mixing theta_thermal computed for one
    joint's geometry/current level with a mismatched rated_current from
    an unrelated scenario produces a critical_r that can sit BELOW the
    joint's own healthy baseline resistance (verified this failure mode
    directly while testing this function - it's a real trap, not a
    hypothetical one). In production this is naturally avoided because
    both values are looked up together per-breaker in the same config
    call - just don't ever hand this function theta_thermal from one
    joint and rated_current from another.
    """
    return critical_delta_t_c / (rated_current_amps ** 2 * theta_thermal_k_per_w)


def projected_days_to_critical(current_r_ohms, critical_r_ohms, dr_dt_ohms_per_day):
    """
    Simple linear extrapolation: at the current rate of drift, how many
    days until R crosses critical_r_ohms. Returns None (not "infinity"
    or a large number) when dr_dt is zero/negative - there is nothing
    meaningful to project for a joint that isn't degrading, and a caller
    silently defaulting a None to 0 or treating it as "far away" would
    both be wrong in different ways, so this is a case Layer 5's prompt
    template needs to handle explicitly (e.g. "not currently degrading",
    not "N/A" or "0 days").

    Returns 0.0 if already at or past critical - this is a real,
    distinct case from "not degrading" and should read as urgent, not
    identical to the "no signal" None case above.

    Verified: a joint drifting at the full 45-day simulation's rate,
    checked at day 20 (well before the 45-day window's end), correctly
    projected ~3.2 days to critical - i.e. this genuinely front-runs the
    danger point instead of just reporting it after the fact, which is
    the entire premise of building this layer instead of alerting off
    raw temperature.
    """
    if dr_dt_ohms_per_day is None or dr_dt_ohms_per_day <= 0:
        return None
    if current_r_ohms >= critical_r_ohms:
        return 0.0
    return (critical_r_ohms - current_r_ohms) / dr_dt_ohms_per_day


def severity_band_from_residual(residual_c):
    """
    Delta-T method severity bands (NETA/thermography-industry-style
    scale, per the shared architecture context), applied to
    RESIDUAL - i.e. measured T minus this joint's OWN physics-expected T
    at its OWN learned-healthy resistance - never to raw absolute
    temperature.

    This distinction matters more than it might look: applying these
    exact same band cutoffs to raw T_hotspot instead of residual was
    tried and verified to be WRONG during testing - a joint that is
    perfectly healthy and non-drifting (CUSUM=0.0 for 45 straight days)
    but simply runs warm under normal load due to its own physical
    geometry produced a raw temperature rise that misclassified as
    "serious" under these same thresholds. Once corrected to use
    Layer 3's actual residual_c definition (which nets out that joint's
    own expected-at-baseline behavior), the same healthy joint correctly
    read "healthy" throughout, and only the genuinely drifting joint
    climbed through the bands. Layer 5 and any dashboard MUST only ever
    feed this function residual_c, never t_hotspot or t_ambient directly.
    """
    if residual_c is None:
        return "unknown"
    if residual_c < 1.0:
        return "healthy"
    elif residual_c < 4.0:
        return "minor"
    elif residual_c < 15.0:
        return "intermediate"
    elif residual_c < 40.0:
        return "serious"
    else:
        return "emergency"
