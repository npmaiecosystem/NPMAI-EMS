"""
Pure mapping from severity_band (Layer 4/5's output) to what kind of
notification this deserves - kept as one small, obviously-correct
function since this is exactly the kind of decision worth being able to
read and verify at a glance, not derive from a notification library's
generic priority levels.
"""
from enum import Enum


class NotificationDisposition(Enum):
    PUSH_HIGH_PRIORITY = "push_high_priority"   # emergency: wake the device, sound override
    PUSH_NORMAL = "push_normal"                 # serious: a normal push, no override
    IN_APP_ONLY = "in_app_only"                 # minor/intermediate: goes in notification_history for the app's alert feed, no push at all
    SUPPRESS = "suppress"                       # healthy/unknown: nothing happens, not even an in-app entry


def disposition_for_severity(severity_band: str) -> NotificationDisposition:
    """
    The line between 'serious' getting a normal push and 'intermediate'
    getting no push at all is a deliberate choice, not an accident of
    the enum ordering: 'intermediate' (4-15C residual, per the Delta-T
    scale) is exactly the range where a joint is drifting but likely has
    real days-to-weeks of runway before it's dangerous - waking someone
    up or interrupting their day for that is the "no push spam for
    low-severity items" case explicitly called out in the original
    requirement. It still needs to be VISIBLE (in_app_only), just not
    urgent.
    """
    if severity_band == "emergency":
        return NotificationDisposition.PUSH_HIGH_PRIORITY
    elif severity_band == "serious":
        return NotificationDisposition.PUSH_NORMAL
    elif severity_band in ("minor", "intermediate"):
        return NotificationDisposition.IN_APP_ONLY
    else:  # healthy, baselining, unknown
        return NotificationDisposition.SUPPRESS
