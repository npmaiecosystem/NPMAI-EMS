#pragma once
#include <Arduino.h>

// Thin driver for the TCA9548A 8-channel I2C multiplexer. We use this
// because every MLX90640 in the system shares the same fixed I2C
// address (0x33) - the only way to talk to more than one on a single
// bus is to isolate them behind separate mux channels and switch which
// one is "connected" before each transaction.
class I2CMux {
public:
  explicit I2CMux(uint8_t i2cAddress = 0x70);
  void begin();
  // Selects channel 0-7. Returns false on I2C failure (mux missing/wiring fault).
  bool selectChannel(uint8_t channel);
  void disableAll();

private:
  uint8_t _addr;
  int8_t _lastChannel; // cache to skip redundant I2C writes when already on this channel
};
