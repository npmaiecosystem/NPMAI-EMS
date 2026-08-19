#include <math.h>
#include "config.h"
#include "Reading.h"
#include "I2CMux.h"
#include "CurrentSensor.h"
#include "ThermalSensor.h"
#include "Failsafe.h"
#include "WiFiManager.h"
#include "TimeSync.h"
#include "RingBuffer.h"
#include "MQTTHandler.h"

I2CMux        mux(TCA9548A_ADDR);
CurrentSensor currentSensor;
ThermalSensor thermalSensor;
Failsafe      failsafe;      
WiFiManager   wifiManager;
TimeSync      timeSync;
RingBuffer    ringBuffer;
MQTTHandler   mqttHandler;

static float thermalFrame[MLX90640_ROWS * MLX90640_COLS];

struct BreakerAccumulator {
  double sumI = 0, sumTh = 0, sumTa = 0;
  uint16_t samples = 0;
};
static BreakerAccumulator accum[NUM_NODES][MAX_BREAKERS_PER_NODE];

unsigned long lastSampleMs = 0;
unsigned long lastSendMs = 0;

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("NPMAI Predictive Sensor - Layer 1 edge firmware booting...");

  failsafe.begin();

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(I2C_CLOCK_HZ);
  mux.begin();

  for (uint8_t n = 0; n < NUM_NODES; n++) {
    MonitoringNode &node = NODES[n];
    if (!mux.selectChannel(node.mux_channel)) {
      Serial.printf("[BOOT] Node %u: mux channel %u select FAILED (mux missing/wiring fault?)\n",
                    n, node.mux_channel);
      continue;
    }
    bool adsOk = currentSensor.begin(node.ads1115_addr, node.ads_gain);
    bool mlxOk = thermalSensor.begin(node.mlx90640_addr);
    Serial.printf("[BOOT] Node %u (mux ch %u): ADS1115 0x%02X %s, MLX90640 0x%02X %s\n",
                  n, node.mux_channel,
                  node.ads1115_addr, adsOk ? "OK" : "MISSING",
                  node.mlx90640_addr, mlxOk ? "OK" : "MISSING");
  }

  wifiManager.begin();
  if (wifiManager.isConnected()) {
    Serial.println("[BOOT] WiFi connected, syncing NTP...");
    if (!timeSync.begin()) {
      Serial.println("[BOOT] NTP sync timed out - will keep the system clock's best effort"
                      " and readings will carry timestamp=0 until it eventually syncs.");
    }
  } else {
    Serial.println("[BOOT] WiFi not connected at boot - will keep retrying in the background."
                    " The local thermal failsafe is fully active regardless of network state.");
  }

  mqttHandler.begin();

  lastSampleMs = millis();
  lastSendMs = millis();

  Serial.println("[BOOT] Setup complete, entering main loop.");
}

void loop() {
  wifiManager.update();
  mqttHandler.update();

  unsigned long now = millis();

  if (now - lastSampleMs >= SAMPLE_INTERVAL_MS) {
    lastSampleMs = now;

    for (uint8_t n = 0; n < NUM_NODES; n++) {
      MonitoringNode &node = NODES[n];

      if (!mux.selectChannel(node.mux_channel)) {
        Serial.printf("[SAMPLE] Node %u: mux select failed, skipping this node this tick\n", n);
        continue;
      }

      bool adsOk = currentSensor.begin(node.ads1115_addr, node.ads_gain);
      if (!adsOk) {
        Serial.printf("[SAMPLE] Node %u: ADS1115 not responding, skipping current reads\n", n);
      }

      bool mlxOk = thermalSensor.begin(node.mlx90640_addr);
      bool frameOk = mlxOk && thermalSensor.readFrame(thermalFrame);
      if (!frameOk) {
        Serial.printf("[SAMPLE] Node %u: MLX90640 frame read failed - thermal/failsafe"
                      " skipped for this node this tick, current will still be read\n", n);
      }

      float ambientC = frameOk
        ? ThermalSensor::zoneAvg(thermalFrame, node.ambient_row_start, node.ambient_row_end,
                                  node.ambient_col_start, node.ambient_col_end)
        : NAN;

      for (uint8_t b = 0; b < node.num_breakers; b++) {
        BreakerZone &bz = node.breakers[b];

        float iRms = adsOk
          ? currentSensor.readRMSCurrent(bz.ads_channel, node.ct_burden_ohms, node.ct_ratio)
          : NAN;

        float hotspotC = frameOk
          ? ThermalSensor::zoneMax(thermalFrame, bz.thermal_row_start, bz.thermal_row_end,
                                    bz.thermal_col_start, bz.thermal_col_end)
          : NAN;

      
        if (frameOk) {
          failsafe.evaluate(hotspotC, bz.failsafe_temp_c);
        }

        BreakerAccumulator &a = accum[n][b];
        if (adsOk && !isnan(iRms)) {
          a.sumI += iRms;
        }
        if (frameOk) {
          a.sumTh += hotspotC;
          a.sumTa += ambientC;
        }
        a.samples++;
      }
    }
  }

  if (now - lastSendMs >= SEND_INTERVAL_MS) {
    lastSendMs = now;

    for (uint8_t n = 0; n < NUM_NODES; n++) {
      MonitoringNode &node = NODES[n];
      for (uint8_t b = 0; b < node.num_breakers; b++) {
        BreakerAccumulator &a = accum[n][b];
        if (a.samples == 0) continue;

        Reading r;
        strlcpy(r.device_id, DEVICE_ID, MAX_ID_LEN);
        strlcpy(r.panel_id, PANEL_ID, MAX_ID_LEN);
        strlcpy(r.breaker_id, node.breakers[b].breaker_id, MAX_ID_LEN);
        r.timestamp  = timeSync.nowUnix(); // 0 if NTP has never synced - see TimeSync.h
        r.I_rms      = (float)(a.sumI / a.samples);
        r.T_hotspot  = (float)(a.sumTh / a.samples);
        r.T_ambient  = (float)(a.sumTa / a.samples);
        r.wifi_rssi  = wifiManager.rssi();

        bool sent = mqttHandler.isConnected() && mqttHandler.publish(r);
        if (!sent) {
          if (!ringBuffer.push(r)) {
            Serial.printf("[SEND] Ring buffer full (%u/%u) - dropping this reading for %s\n",
                          ringBuffer.count(), RING_BUFFER_CAPACITY, r.breaker_id);
          }
        }

        a = BreakerAccumulator();
      }
    }

    const uint16_t MAX_FLUSH_PER_TICK = 20;
    uint16_t flushed = 0;
    Reading buffered;
    while (mqttHandler.isConnected() && flushed < MAX_FLUSH_PER_TICK && ringBuffer.pop(buffered)) {
      if (!mqttHandler.publish(buffered)) {

        ringBuffer.push(buffered);
        break;
      }
      flushed++;
    }
    if (flushed > 0) {
      Serial.printf("[SEND] Flushed %u buffered reading(s), %u remaining\n",
                    flushed, ringBuffer.count());
    }
  }
}
