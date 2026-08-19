#pragma once
#include <Arduino.h>
#include "config.h"

// This is the ONE hard safety guarantee in the whole 7-layer system: it
// must work with zero WiFi, zero MQTT, zero cloud connectivity, and zero
// dependency on anything upstream. It is called directly from the main
// sensor-read loop on every sample tick - never routed through any
// network callback, queue, or async handler that could stall or drop it.
class Failsafe {
public:
  void begin();

  // Call immediately after every thermal reading for a given breaker.
  // thresholdC <= 0 means "use FAILSAFE_TEMP_C_DEFAULT".
  void evaluate(float tHotspotC, float thresholdC = 0);

  bool isTripped() const { return _tripped; }
  void manualReset();

private:
  bool _tripped = false;
};
