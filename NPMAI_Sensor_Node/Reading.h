#pragma once
#include <Arduino.h>

// Fixed-size char buffers instead of Arduino String on purpose: String
// does heap alloc/free on every assignment/concat, which fragments the
// heap over weeks of continuous uptime on a memory-constrained device.
// Fixed buffers avoid that entirely at the cost of a hard length cap.
#define MAX_ID_LEN 24

struct Reading {
  char device_id[MAX_ID_LEN];
  char panel_id[MAX_ID_LEN];
  char breaker_id[MAX_ID_LEN];
  uint32_t timestamp;   // unix epoch seconds, from NTP (0 = never synced)
  float I_rms;          // amps, primary-side RMS current
  float T_hotspot;      // deg C, max pixel in this breaker's thermal zone
  float T_ambient;      // deg C, avg pixel in the node's ambient reference zone
  int8_t wifi_rssi;     // dBm, 0 if not connected at send time
};
