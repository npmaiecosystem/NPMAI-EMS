#include "CurrentSensor.h"

CurrentSensor::CurrentSensor() {}

bool CurrentSensor::begin(uint8_t i2c_addr, adsGain_t gain) {
  if (!_ads.begin(i2c_addr, &Wire)) {
    return false;
  }
  _ads.setGain(gain);
  // 860 SPS is the fastest data rate the ADS1115 supports in single-shot
  // mode. We need enough samples across at least one full AC cycle
  // (20ms @ 50Hz mains, 16.7ms @ 60Hz) to reconstruct the waveform shape
  // well enough for an accurate RMS estimate - the faster the data rate,
  // the more samples we can fit per cycle without stretching the read
  // out over multiple seconds.
  _ads.setDataRate(RATE_ADS1115_860SPS);
  return true;
}

float CurrentSensor::readRMSCurrent(uint8_t ads_channel, float burden_ohms, float ct_ratio) {
  // ============================================================
  // True RMS current calculation - the math, explained
  // ============================================================
  //
  // A CT (current transformer) clamp induces a small AC current in its
  // secondary winding proportional to the large AC current flowing
  // through the primary conductor (the breaker's wire) passing through
  // its core, scaled down by the CT's turns ratio (e.g. a "2000:1" CT
  // reports 1/2000th of the primary current on its secondary). A burden
  // resistor across the secondary converts that AC current into an AC
  // voltage: V_burden = I_secondary * R_burden (Ohm's law).
  //
  // That AC voltage swings both positive and negative around 0V, but the
  // ADS1115 in single-ended mode can only measure 0V to +V_ref - it has
  // no negative range. So the sensor board's bias network adds a DC
  // offset (typically VCC/2) to shift the whole waveform positive before
  // it reaches the ADC. Every raw sample is therefore:
  //
  //     sample = DC_bias + AC_swing(t)
  //
  // We CANNOT just average the raw samples to get a useful current
  // reading - the average of a sine wave over a full cycle is its DC
  // bias, which tells us nothing about current magnitude. Instead we
  // compute the actual mathematical definition of RMS (Root Mean
  // Square), which is why it's called "RMS" and not "average":
  //
  //   1. Remove the DC bias from each sample -> isolates AC_swing(t).
  //   2. Square each AC sample -> makes every value positive (so summing
  //      doesn't cancel positive/negative excursions to zero) and
  //      weights larger excursions more heavily than smaller ones. This
  //      isn't arbitrary: power dissipated as heat scales with I^2*R,
  //      the exact physics this whole system is built to detect, so
  //      RMS is the electrically meaningful "effective" current level.
  //   3. Average (Mean) the squared values across the sampling window.
  //   4. Take the Square Root to return from squared-volts back to volts.
  //
  // The one tricky part is step 1: we don't know the exact DC bias in
  // advance (it drifts slightly with supply voltage and temperature), so
  // instead of subtracting a fixed constant we track it live with an
  // exponential moving average - a simple adaptive low-pass filter. Each
  // new sample nudges the tracked offset a small step toward itself:
  //
  //     filteredOffset += (sample - filteredOffset) / ALPHA
  //
  // A larger ALPHA makes the filter move more slowly. We want it slow
  // enough that it doesn't chase the fast-moving AC waveform itself
  // (which would erase the very signal we're trying to measure) but
  // fast enough to track real bias drift over seconds/minutes. This
  // adaptive-offset-then-RMS technique is the same approach used by the
  // widely-deployed OpenEnergyMonitor EmonLib and is well proven for
  // CT-clamp current sensing on 16-bit ADCs.

  // Seed the offset filter with one real sample rather than 0, so it
  // starts close to the true bias instead of needing hundreds of samples
  // to walk up/down from an arbitrary starting point.
  double filteredOffsetVolts = _ads.computeVolts(_ads.readADC_SingleEnded(ads_channel));
  double sumSquares = 0.0;

  for (uint16_t i = 0; i < CT_RMS_SAMPLE_COUNT; i++) {
    int16_t raw = _ads.readADC_SingleEnded(ads_channel);
    double sampleVolts = _ads.computeVolts(raw);

    filteredOffsetVolts += (sampleVolts - filteredOffsetVolts) / CT_OFFSET_FILTER_ALPHA;
    double acVolts = sampleVolts - filteredOffsetVolts;

    sumSquares += acVolts * acVolts;
  }

  double meanSquare = sumSquares / (double)CT_RMS_SAMPLE_COUNT;
  double rmsVolts = sqrt(meanSquare); // RMS voltage developed across the burden resistor

  // Ohm's law, rearranged: I_secondary(rms) = V_burden(rms) / R_burden
  double rmsCurrentSecondary = rmsVolts / burden_ohms;

  // Scale back up by the CT's turns ratio to recover the primary
  // (breaker) current. e.g. with a 2000:1 CT, 50mA RMS on the secondary
  // means 100A RMS is flowing through the primary conductor.
  double rmsCurrentPrimary = rmsCurrentSecondary * ct_ratio;

  return (float)rmsCurrentPrimary;
}
