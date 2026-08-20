"""
Per-breaker thermal-model geometry, mirroring how config.h's NODES table
holds per-breaker CALIBRATION values on the Layer 1 firmware side.
There's no live coupling between the two - this is Layer 3's own config,
kept in Python so it can move to a `breaker_thermal_config` DB table
later without touching any of the physics math. At the pilot's scale
(dozens of breakers) a static dict is the simplest thing that works.

`area_m2` / `char_length_m` describe the joint/terminal's exposed
surface - same measurement philosophy as config.h's thermal zone pixel
ranges: measure the actual lug + surrounding busbar area visible to the
thermal camera. The defaults below are a generic placeholder (a typical
60-200A compression lug), same spirit as config.h's placeholder CT ratio
and thermal zones - review and override per site as real joints get
inspected.

`theta_thermal_override_k_per_w`, if set, skips the CoolProp/
Churchill-Chu correlation entirely (see app/physics/thermal_model.py)
and uses this fixed value instead. Use it if you have (or later derive
from field data) a directly-measured thermal resistance for a specific
joint that you trust more than the general correlation - the
correlation is a reasonable physically-grounded default, not a claim
that it beats real calibration data.
"""
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from app.config import settings


@dataclass(frozen=True)
class BreakerThermalConfig:
    area_m2: float
    char_length_m: float
    emissivity: float
    theta_thermal_override_k_per_w: Optional[float] = None


# (panel_id, breaker_id) -> overrides. Empty by default for the pilot -
# add entries here as specific joints get measured/calibrated, e.g.:
#
#   ("panel-2F-main", "brk-01"): BreakerThermalConfig(
#       area_m2=0.002, char_length_m=0.025, emissivity=0.9,
#   ),
_OVERRIDES: Dict[Tuple[str, str], BreakerThermalConfig] = {}


def get_breaker_thermal_config(panel_id: str, breaker_id: str) -> BreakerThermalConfig:
    override = _OVERRIDES.get((panel_id, breaker_id))
    if override is not None:
        return override
    return BreakerThermalConfig(
        area_m2=settings.default_joint_area_m2,
        char_length_m=settings.default_joint_char_length_m,
        emissivity=settings.default_joint_emissivity,
    )
