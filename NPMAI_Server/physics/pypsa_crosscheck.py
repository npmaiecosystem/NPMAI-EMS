"""
Independent QA cross-check on R_effective (app/physics/thermal_model.py):
build a trivial two-bus PyPSA network with a Line whose resistance is
R_effective and a Load equal to the I^2*R power it should be dissipating,
run an AC power flow, and compare PyPSA's own computed line loss back
against I^2*R. For a simple resistive branch these should agree almost
exactly (ratio close to 1.0) - divergence signals either a numerical
issue in our own R_effective calc or genuinely implausible input.

This is NOT part of the core pipeline: it doesn't gate baseline
completion, doesn't affect residual/status, and its failure never marks
a reading 'physics_failed' (see app/physics_processor.py). It's a QA
signal recorded alongside the row (derived_metrics.pypsa_cross_check_ratio)
for anyone auditing the physics later - the same spirit as the pilot's
own manual "ratio ~1.01" validation.

---- Units gotcha (pinned pypsa==1.2.4) ----

PyPSA bus voltages are in kV and all real/reactive power quantities
(p_set, s_nom, etc.) are in MW/MVAr by default - NOT kW/V. Our loss
figure starts in Watts (I_rms^2 * R_effective), so it's divided by 1e6
(W -> MW) before it goes anywhere near p_set. Skipping this conversion
is exactly what previously produced a "Matrix is exactly singular" error
in testing: PyPSA doesn't reject wrong units, it just hands the solver a
power-flow problem whose real answer requires an effectively-infinite
voltage/reactive response, and the AC power flow's admittance matrix
becomes numerically singular trying to solve it.
"""
import logging

import pypsa

logger = logging.getLogger(__name__)


class CrossCheckError(Exception):
    """Raised when the PyPSA cross-check network fails to build or solve.
    Always non-fatal to the caller - see module docstring."""


def cross_check_loss_ratio(i_rms: float, r_effective_ohms, v_nom_kv: float = 0.4):
    """
    Returns (our I^2*R loss) / (PyPSA's computed line loss), which should
    sit close to 1.0. Returns None when there's nothing meaningful to
    check (zero current or zero resistance) rather than raising - that's
    a legitimate "nothing to cross-check", not a failure.
    """
    if r_effective_ohms is None or r_effective_ohms <= 0 or i_rms is None or i_rms <= 0:
        return None

    p_loss_w = i_rms ** 2 * r_effective_ohms
    p_loss_mw = p_loss_w / 1_000_000.0  # W -> MW, see module docstring

    try:
        n = pypsa.Network()
        n.add("Bus", "source", v_nom=v_nom_kv)
        n.add("Bus", "joint", v_nom=v_nom_kv)
        n.add(
            "Line", "joint_conductor",
            bus0="source", bus1="joint",
            r=r_effective_ohms,
            # Small non-zero reactance keeps the admittance matrix
            # well-conditioned - a purely-resistive (x=0) line is a
            # common contributor to the singular-matrix failure mode
            # noted above.
            x=max(r_effective_ohms * 0.01, 1e-6),
            s_nom=1.0,
        )
        n.add("Generator", "incomer", bus="source", control="Slack")
        n.add("Load", "joint_load", bus="joint", p_set=p_loss_mw, q_set=0.0)

        n.pf()

        p0 = n.lines_t.p0["joint_conductor"].iloc[0]
        p1 = n.lines_t.p1["joint_conductor"].iloc[0]
        line_loss_mw = abs(p0 + p1)

        if line_loss_mw != line_loss_mw or line_loss_mw <= 0:  # NaN check, no numpy import needed
            raise CrossCheckError("PyPSA returned a non-finite or zero line loss")

        return p_loss_mw / line_loss_mw

    except CrossCheckError:
        raise
    except Exception as exc:
        logger.debug("PyPSA cross-check raised for i_rms=%s r_effective=%s: %s", i_rms, r_effective_ohms, exc)
        raise CrossCheckError(str(exc)) from exc
