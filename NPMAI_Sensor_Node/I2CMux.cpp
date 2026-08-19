#include "I2CMux.h"
#include <Wire.h>

I2CMux::I2CMux(uint8_t i2cAddress) : _addr(i2cAddress), _lastChannel(-1) {}

void I2CMux::begin() {
  disableAll();
}

bool I2CMux::selectChannel(uint8_t channel) {
  if (channel > 7) return false;

  // Skip the I2C transaction if we're already parked on this channel -
  // the TCA9548A latches its channel selection until told otherwise, so
  // re-sending the same byte every call would just be wasted bus time.
  if (_lastChannel == (int8_t)channel) return true;

  // --- Multiplexer addressing logic ---
  // The TCA9548A has ONE control register, written as a single byte over
  // I2C to its own address (default 0x70, set by the A0-A2 pins). Each
  // of the 8 bits in that byte independently gates one downstream SDx/SCx
  // pair onto the shared upstream SDA/SCL bus:
  //
  //   bit 0 (0x01) -> channel 0    bit 4 (0x10) -> channel 4
  //   bit 1 (0x02) -> channel 1    bit 5 (0x20) -> channel 5
  //   bit 2 (0x04) -> channel 2    bit 6 (0x40) -> channel 6
  //   bit 3 (0x08) -> channel 3    bit 7 (0x80) -> channel 7
  //
  // Writing (1 << channel) sets exactly one bit, e.g. channel 3 becomes
  // 0b00001000 = 0x08, opening ONLY that channel's pair to the bus while
  // every other channel stays isolated (high-impedance, invisible to the
  // master). We deliberately select one channel at a time rather than
  // OR-ing bits together: every MLX90640 behind this mux answers to the
  // identical fixed address 0x33, so if two channels were open at once,
  // both thermal cameras would respond to the same address simultaneously
  // and corrupt the transaction (bus contention / garbled ACKs).
  Wire.beginTransmission(_addr);
  Wire.write((uint8_t)(1 << channel));
  uint8_t result = Wire.endTransmission();

  if (result != 0) {
    // Non-zero = NACK or bus error - most likely the mux isn't present
    // at _addr, or a wiring fault. Invalidate our cached channel so the
    // next call retries instead of trusting a selection that may not
    // have taken effect.
    _lastChannel = -1;
    return false;
  }

  _lastChannel = channel;
  return true;
}

void I2CMux::disableAll() {
  Wire.beginTransmission(_addr);
  Wire.write((uint8_t)0x00); // all channel bits cleared -> every downstream bus isolated
  Wire.endTransmission();
  _lastChannel = -1;
}
