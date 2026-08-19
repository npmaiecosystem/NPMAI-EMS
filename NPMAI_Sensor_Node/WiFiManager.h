#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include "config.h"

class WiFiManager {
public:
  // Bounded blocking attempt at boot only (max WIFI_CONNECT_TIMEOUT_MS).
  // We never block indefinitely here, because loop() - where the local
  // thermal failsafe runs - must be reached even if WiFi never comes up.
  void begin();

  // Call every loop() iteration. Non-blocking: if disconnected, retries
  // at most once per WIFI_RECONNECT_INTERVAL_MS and returns immediately
  // either way.
  void update();

  bool isConnected() const;
  int8_t rssi() const;

private:
  unsigned long _lastAttemptMs = 0;
};
