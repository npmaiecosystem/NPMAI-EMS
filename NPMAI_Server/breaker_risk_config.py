"""
Per-breaker risk parameters - rated current and critical temperature
rise - mirroring exactly how app/breaker_thermal_config.py holds
per-breaker thermal GEOMETRY. Kept as a separate config module (not
merged into breaker_thermal_config.py) because these answer a different
question: breaker_thermal_config.py describes the joint's physical
thermal behavior (used by Layer 3 to compute R_effective/residual at
all); this module describes what THRESHOLD counts as dangerous for that
specific breaker (used by Layer 4 to decide WHEN to alert). A breaker's
physical geometry and its rated current capacity are independent facts
about it - both need their own override slot.

CRITICAL correctness note (found and verified while building
feature_math.py): theta_thermal (from breaker_thermal_config.py) and
rated_current_amps (from here) get passed together into
critical_r_for_breaker() - if you ever compute one from a different
breaker's data than the other, the result can be physically nonsensical
(a critical resistance threshold BELOW that joint's own healthy
baseline). app/feature_processor.py always looks both up for the SAME
(panel_id, breaker_id) in the same call, so this can't happen in the
pipeline itself - but if you're ever calling feature_math.py functions
directly (e.g. in a notebook, in Layer 5, in a dashboard calc), always
pull both values for the same joint together, never mix and match.
"""
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from app.config import settings


@dataclass(frozen=True)
class BreakerRiskConfig:
    rated_current_amps: float
    critical_delta_t_c: float


# (panel_id, breaker_id) -> overrides. Empty by default for the pilot -
# add entries here as real breakers' actual rated capacity (from the
# panel schedule/breaker nameplate) gets recorded, e.g.:
#
#   ("panel-2F-main", "brk-01"): BreakerRiskConfig(
#       rated_current_amps=32.0, critical_delta_t_c=15.0,
#   ),
_OVERRIDES: Dict[Tuple[str, str], BreakerRiskConfig] = {}


def get_breaker_risk_config(panel_id: str, breaker_id: str) -> BreakerRiskConfig:
    override = _OVERRIDES.get((panel_id, breaker_id))
    if override is not None:
        return override
    return BreakerRiskConfig(
        rated_current_amps=settings.default_rated_current_amps,
        critical_delta_t_c=settings.default_critical_delta_t_c,
    )
