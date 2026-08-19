#pragma once
#include <Arduino.h>
#include <Adafruit_ADS1X15.h>
#include "config.h"

class CurrentSensor {
public:
  CurrentSensor();

  // (Re)binds this object to the ADS1115 at i2c_addr. The caller must
  // have already selected the correct mux channel. Cheap to call every
  // sample tick - it's just an address/config write, not a calibration
  // load (unlike the MLX90640, see ThermalSensor.h).
  bool begin(uint8_t i2c_addr, adsGain_t gain);

  // Blocking. Takes CT_RMS_SAMPLE_COUNT samples on ads_channel (0-3) and
  // returns the true RMS primary-side current in Amps, using the given
  // burden resistor and CT turns ratio. See CurrentSensor.cpp for the
  // full RMS derivation.
  float readRMSCurrent(uint8_t ads_channel, float burden_ohms, float ct_ratio);

private:
  Adafruit_ADS1115 _ads;
};
