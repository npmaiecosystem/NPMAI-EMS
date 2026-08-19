#pragma once
#include <Arduino.h>
#include "Reading.h"
#include "config.h"

// ============================================================
// Buffering strategy: in-RAM circular buffer, NOT SPIFFS/flash.
// Trade-off, so you can revisit this decision for your deployment:
//
//   RAM (implemented here)
//     + Fast - no flash I/O latency in the sample loop.
//     + Zero flash wear - matters over years of continuous operation.
//     + Simple - no partial-write/corruption handling needed, since a
//       reboot just clears it (no stale/half-written state to detect).
//     - Volatile: a power loss or reboot during an outage loses
//       whatever is currently buffered.
//     - Capacity bounded by free heap. After WiFi/MQTT/TLS stacks are
//       up, an ESP32 typically has ~200-300KB free, which caps how long
//       an outage can be bridged before new readings start being
//       dropped (RING_BUFFER_CAPACITY below).
//
//   SPIFFS/LittleFS (flash-backed alternative, not implemented here)
//     + Survives reboots and power loss.
//     + Can hold far more buffered history than fits in RAM.
//     - Flash has a finite write-cycle life (roughly 10,000-100,000
//       erase cycles per sector depending on the chip). Fine at a 60s
//       send interval during a normal outage, but a bug that spins
//       writes in a tight loop could wear cells out over time.
//     - Needs an append-only log format with a read/write pointer that
//       survives power loss mid-write without corrupting the file -
//       more code, more edge cases.
//
// Given this pilot expects WiFi/broker outages on the order of minutes
// to a few hours (not multi-day), and losing a few hours of buffered
// slow-trend data on a coincident power cut is an acceptable
// degradation (Layer 4's EWMA/CUSUM baselines re-settle from live data
// once readings resume), RAM is the right default. If your site sees
// longer outages, or power cuts routinely coincide with network cuts,
// swap this module for a LittleFS-backed version - the public
// interface (push/pop/count) is intentionally the only thing
// MQTTHandler and main.ino depend on, so that swap shouldn't require
// touching either of those files.
// ============================================================
class RingBuffer {
public:
  RingBuffer();

  // Returns false if the buffer is full - the new reading is dropped,
  // NOT the oldest one (see .cpp for why).
  bool push(const Reading& r);

  // FIFO order. Returns false if empty.
  bool pop(Reading& out);

  uint16_t count() const;
  bool isFull() const;
  bool isEmpty() const;

private:
  Reading _buf[RING_BUFFER_CAPACITY];
  uint16_t _head; // next write index
  uint16_t _tail; // next read index
  uint16_t _count;
};
