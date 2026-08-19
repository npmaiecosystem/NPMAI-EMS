#include "ThermalSensor.h"

ThermalSensor::ThermalSensor() {}

bool ThermalSensor::begin(uint8_t i2c_addr) {
  if (!_mlx.begin(i2c_addr, &Wire)) {
    return false;
  }
  _mlx.setMode(MLX90640_CHESS);           // chess (checkerboard) subpage pattern averages noise better than interleaved rows for static scenes
  _mlx.setResolution(MLX90640_ADC_18BIT); // highest ADC resolution the sensor supports
  _mlx.setRefreshRate(MLX90640_4_HZ);     // 4Hz balances frame read time against I2C bus loading across up to 8 muxed units
  return true;
}

bool ThermalSensor::readFrame(float* frameBuffer) {
  // getFrame() blocks until both sub-pages of a full frame have been
  // read over I2C and returns 0 on success, negative on I2C/CRC failure
  // (e.g. a sensor that dropped off the bus mid-read).
  return _mlx.getFrame(frameBuffer) == 0;
}

float ThermalSensor::zoneMax(const float* frameBuffer,
                              uint8_t rowStart, uint8_t rowEnd,
                              uint8_t colStart, uint8_t colEnd) {
  float maxVal = -274.0f; // colder than absolute zero (-273.15C) - guaranteed to be overwritten by any real pixel
  for (uint8_t r = rowStart; r <= rowEnd; r++) {
    for (uint8_t c = colStart; c <= colEnd; c++) {
      // Row-major indexing: the Adafruit MLX90640 library fills a
      // 32-pixel-wide x 24-pixel-tall grid one full row at a time, so
      // pixel (row, col) lives at flat index (row * MLX90640_COLS + col).
      float v = frameBuffer[(uint16_t)r * MLX90640_COLS + c];
      if (v > maxVal) maxVal = v;
    }
  }
  return maxVal;
}

float ThermalSensor::zoneAvg(const float* frameBuffer,
                              uint8_t rowStart, uint8_t rowEnd,
                              uint8_t colStart, uint8_t colEnd) {
  double sum = 0.0;
  uint32_t count = 0;
  for (uint8_t r = rowStart; r <= rowEnd; r++) {
    for (uint8_t c = colStart; c <= colEnd; c++) {
      sum += frameBuffer[(uint16_t)r * MLX90640_COLS + c];
      count++;
    }
  }
  return count ? (float)(sum / count) : 0.0f;
}
