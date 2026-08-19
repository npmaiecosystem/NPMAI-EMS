"""
Forward model: what T_hotspot would a HEALTHY joint (R_effective =
r_nominal_ohms, this joint's own learned baseline - see app/baseline.py)
show at the current I_rms? T_expected feeds
residual = T_hotspot_measured - T_expected, the real fault signal (see
app/physics/thermal_model.py's module docstring for why raw temperature
alone isn't enough).

---- Why TESPy for what's otherwise a two-line algebra problem ----

At steady state, delta_T_expected = Q_expected / (m_dot * cp) is just as
closed-form as the R_effective back-calc in thermal_model.py -
expected_delta_t_closed_form() there computes exactly that, with no
TESPy involved, specifically so the pytest suite has a library-free
reference answer to check this module against.

The service pipeline (app/physics_processor.py) routes through TESPy
anyway, for two reasons: (1) it's a real energy-balance solver, not
hand-rolled algebra, so it converges or fails on its own terms - which
is exactly the resilience case requirement #7 asks for (a non-convergent
solve here becomes a 'physics_failed' row, not a crashed service, and
R_effective - computed independently in thermal_model.py - is still
recorded even when this fails); (2) it gives this layer a real network
we can extend later (e.g. multiple joints on a shared bus duct
exchanging heat, mixing points at a riser) without re-deriving the
algebra by hand, the same "don't rewrite the public interface, extend
the model" philosophy already used elsewhere in this project (see
app/batch_writer.py's note on the COPY-vs-executemany scaling path).

We model the surrounding air as a fictitious convective stream: a
Source supplies air at T_ambient, a SimpleHeatExchanger absorbs
Q_expected = I_rms^2 * r_nominal_ohms (the healthy-joint dissipation),
and a Sink receives the warmed air. The stream's mass flow is chosen so
the resulting temperature rise reproduces the SAME theta_thermal used in
the closed-form back-calc:

    m_dot = 1 / (theta_thermal * cp_air)

This is what makes "expected" (this module) and "actual" (the back-calc
in thermal_model.py) directly comparable as a residual: they share one
physical thermal conductance, just applied in opposite directions,
instead of two independently-calibrated models that would make the
residual partly an artifact of model mismatch rather than a real fault
signal.

---- Units: raw SI, per the pinned-version gotcha ----

TESPy 0.11.x deprecated Network(p_unit=..., T_unit=...). Rather than
adopt the newer nw.units.set_defaults() API (whose exact keyword names
have moved between 0.11 point releases), we take the simpler path the
gotcha already points at: skip unit customization entirely and work
directly in TESPy's raw SI base units - Kelvin, Pascal, kg/s, Watts. No
unit-name guessing, no risk of a future TESPy release moving those
kwargs again.
"""
import logging

from tespy.components import Sink, Source, SimpleHeatExchanger
from tespy.connections import Connection
from tespy.networks import Network

logger = logging.getLogger(__name__)

_CP_AIR_J_PER_KG_K = 1006.0  # air specific heat near room temperature.
# A constant here (rather than another CoolProp call) is deliberate: it
# keeps the fictitious air stream's mass flow a simple, always-computable
# number that doesn't depend on TESPy's own internal fluid-property
# lookups converging first - one less thing that can fail before we even
# reach nw.solve(). cp of air varies well under 1% across the -20C to
# 80C range this system operates in, negligible next to the other
# approximations already baked into theta_thermal.


class PhysicsSolveError(Exception):
    """Raised whenever the TESPy expected-T solve can't produce a trustworthy
    answer - bad input, non-convergence, or an unexpected library error.
    Callers (app/physics_processor.py) catch this specifically and mark
    the reading 'physics_failed' instead of losing it or crashing the
    service."""


def compute_expected_t_tespy(
    i_rms: float,
    r_nominal_ohms: float,
    theta_thermal: float,
    t_ambient_c: float,
) -> float:
    if theta_thermal is None or theta_thermal <= 0:
        raise PhysicsSolveError(f"theta_thermal must be positive, got {theta_thermal!r}")
    if r_nominal_ohms is None or r_nominal_ohms < 0:
        raise PhysicsSolveError(f"r_nominal_ohms must be non-negative, got {r_nominal_ohms!r}")
    if i_rms is None or i_rms < 0:
        raise PhysicsSolveError(f"i_rms must be non-negative, got {i_rms!r}")

    q_expected_w = i_rms ** 2 * r_nominal_ohms
    if q_expected_w <= 0:
        # No expected dissipation at zero current - the answer is
        # trivially ambient, no need to run the solver at all.
        return t_ambient_c

    m_dot_kg_s = max(1.0 / (theta_thermal * _CP_AIR_J_PER_KG_K), 1e-8)
    t_ambient_k = t_ambient_c + 273.15

    try:
        nw = Network()
        nw.iterinfo = False

        source = Source("ambient_air_in")
        sink = Sink("ambient_air_out")
        joint = SimpleHeatExchanger("joint_thermal_boundary")

        c_in = Connection(source, "out1", joint, "in1", label="c_in")
        c_out = Connection(joint, "out1", sink, "in1", label="c_out")
        nw.add_conns(c_in, c_out)

        c_in.set_attr(fluid={"Air": 1}, T=t_ambient_k, p=101325.0, m=m_dot_kg_s)
        joint.set_attr(Q=q_expected_w, pr=1.0)

        nw.solve("design")

        if not getattr(nw, "converged", False):
            raise PhysicsSolveError("TESPy network did not converge")

        t_out_k = c_out.T.val
        return t_out_k - 273.15

    except PhysicsSolveError:
        raise
    except Exception as exc:
        logger.debug(
            "TESPy solve raised for i_rms=%s r_nominal=%s theta=%s t_ambient=%s: %s",
            i_rms, r_nominal_ohms, theta_thermal, t_ambient_c, exc,
        )
        raise PhysicsSolveError(str(exc)) from exc