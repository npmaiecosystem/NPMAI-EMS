#include "Failsafe.h"

void Failsafe::begin() {
  pinMode(FAILSAFE_GPIO_PIN, OUTPUT);
  digitalWrite(FAILSAFE_GPIO_PIN, FAILSAFE_ACTIVE_HIGH ? LOW : HIGH); // start de-energized
  pinMode(FAILSAFE_RESET_BTN_PIN, INPUT_PULLUP);
  _tripped = false;
}

void Failsafe::evaluate(float tHotspotC, float thresholdC) {
  float threshold = (thresholdC > 0) ? thresholdC : FAILSAFE_TEMP_C_DEFAULT;

  if (!_tripped && tHotspotC >= threshold) {
    _tripped = true;
    digitalWrite(FAILSAFE_GPIO_PIN, FAILSAFE_ACTIVE_HIGH ? HIGH : LOW);
  } else if (_tripped && tHotspotC <= (threshold - FAILSAFE_HYSTERESIS_C)) {
    // Auto-clear only once the temperature has dropped a safe margin
    // below the threshold, to avoid the alarm chattering on/off if the
    // reading hovers right at the boundary. If your site's procedure
    // requires a human to physically walk over and acknowledge every
    // trip, delete this else-if branch and rely on manualReset() only.
    _tripped = false;
    digitalWrite(FAILSAFE_GPIO_PIN, FAILSAFE_ACTIVE_HIGH ? LOW : HIGH);
  }

  // Manual reset button, wired active-low with the internal pullup enabled.
  if (_tripped && digitalRead(FAILSAFE_RESET_BTN_PIN) == LOW) {
    manualReset();
  }
}

void Failsafe::manualReset() {
  _tripped = false;
  digitalWrite(FAILSAFE_GPIO_PIN, FAILSAFE_ACTIVE_HIGH ? LOW : HIGH);
}
