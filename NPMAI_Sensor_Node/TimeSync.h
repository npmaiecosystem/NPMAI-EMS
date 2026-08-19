#pragma once
#include <Arduino.h>
#include "config.h"

class TimeSync {
public:
  // Call once after WiFi connects. Bounded blocking wait (NTP_SYNC_TIMEOUT_MS)
  // for the first successful sync. Returns false if it timed out - the
  // device keeps running either way, and nowUnix() will report 0 until a
  // sync eventually succeeds (readings taken before sync are still sent,
  // just with timestamp=0; downstream ingestion should treat that as
  // "device clock not yet synced" rather than discarding the reading).
  bool begin();

  bool isSynced() const;
  uint32_t nowUnix() const;
};
