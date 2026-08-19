#include "TimeSync.h"
#include <time.h>

bool TimeSync::begin() {
  configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, NTP_SERVER_1, NTP_SERVER_2);

  unsigned long start = millis();
  time_t now = time(nullptr);
  // A freshly-booted ESP32's clock starts at epoch 0 (Jan 1 1970). We
  // treat "later than 2020-01-01" as our cheap signal that a real NTP
  // response has landed, without needing extra sync-state bookkeeping.
  while (now < 1577836800UL && (millis() - start) < NTP_SYNC_TIMEOUT_MS) {
    delay(250);
    now = time(nullptr);
  }
  return isSynced();
}

bool TimeSync::isSynced() const {
  return time(nullptr) >= 1577836800UL;
}

uint32_t TimeSync::nowUnix() const {
  return isSynced() ? (uint32_t)time(nullptr) : 0;
}
