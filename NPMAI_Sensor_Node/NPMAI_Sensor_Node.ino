// =====================================================================
// NPMAI-Predictive Sensor - Layer 1: ESP32 Edge Firmware
//
// Pure acquisition + relay, plus one hardcoded local safety failsafe.
// NO physics (R_effective, EWMA, CUSUM, etc.) is computed on this
// device - that all happens in Layers 3/4 in the cloud. This firmware's
// only jobs are: read sensors accurately, get the data off the device
// reliably (buffering through outages), and guarantee the local
// overtemp failsafe fires no matter what else is or isn't working.
//
// See config.h for every value you need to edit for your site.
// =====================================================================
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
Failsafe      failsafe;      // one shared panel-level alarm output; see note in loop() below
WiFiManager   wifiManager;
TimeSync      timeSync;
RingBuffer    ringBuffer;
MQTTHandler   mqttHandler;

static float thermalFrame[MLX90640_ROWS * MLX90640_COLS];

// Per-breaker accumulators, averaged between SEND_INTERVAL_MS ticks to
// smooth out sample-to-sample noise/jitter rather than transmitting
// every raw SAMPLE_INTERVAL_MS reading individually.
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

  // Failsafe GPIO must be configured before anything else that could
  // possibly hang (WiFi, NTP) gets a chance to.
  failsafe.begin();

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(I2C_CLOCK_HZ);
  mux.begin();

  // Startup diagnostic pass: confirm every configured node's sensors
  // respond. A node failing here is logged but not fatal to boot - a
  // node that drops out later (loose I2C connector, sensor fault) is
  // handled per-reading in the main loop instead of crashing the device.
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
  // --- Non-blocking network maintenance, every loop iteration ---
  wifiManager.update();
  mqttHandler.update();

  unsigned long now = millis();

  // =====================================================================
  // SAMPLE TICK - read every sensor, evaluate the failsafe immediately
  // =====================================================================
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

      // NOTE: thermalSensor.begin() is called every tick, for every
      // node, even though the I2C address (0x33) never changes. This is
      // intentional and required, not wasted overhead - see the comment
      // on ThermalSensor::begin() in ThermalSensor.h. Each mux channel
      // hides a physically different MLX90640 with its own factory
      // calibration EEPROM, and that calibration must be reloaded every
      // time we switch which physical unit we're talking to.
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

        // Blocking, ~0.2-0.4s per breaker at the default CT_RMS_SAMPLE_COUNT
        // (200 samples @ ~860SPS + I2C overhead). Fine here because this
        // whole loop runs once per SAMPLE_INTERVAL_MS (default 10s), not
        // once per loop() iteration - with 12 breakers total that's
        // roughly 2.4-4.8s of blocking work per sample tick, comfortably
        // inside the 10s budget. If you add more breakers/nodes, re-check
        // this budget and raise SAMPLE_INTERVAL_MS if needed.
        float iRms = adsOk
          ? currentSensor.readRMSCurrent(bz.ads_channel, node.ct_burden_ohms, node.ct_ratio)
          : NAN;

        float hotspotC = frameOk
          ? ThermalSensor::zoneMax(thermalFrame, bz.thermal_row_start, bz.thermal_row_end,
                                    bz.thermal_col_start, bz.thermal_col_end)
          : NAN;

        // --- Local safety failsafe ---
        // Evaluated right here, immediately, per breaker, independent of
        // WiFi/MQTT state and independent of the averaging/send logic
        // below. This uses the raw per-sample-interval hotspot reading
        // (every SAMPLE_INTERVAL_MS), NOT the SEND_INTERVAL_MS average -
        // we don't want a 60-second smoothing window standing between a
        // dangerous reading and the alarm firing.
        //
        // NOTE ON SCOPE: `failsafe` is a single shared object driving one
        // GPIO output for the whole panel/device - any breaker crossing
        // its threshold trips the same physical buzzer/relay. If you
        // need to know WHICH breaker tripped without checking the
        // dashboard, or want independent per-breaker alarm outputs,
        // that's a hardware change (one Failsafe instance + one GPIO per
        // breaker) rather than a firmware logic change.
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

  // =====================================================================
  // SEND TICK - average, transmit (or buffer), and opportunistically
  // flush any backlog from a prior outage
  // =====================================================================
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

        // Reset the accumulator for the next averaging window.
        a = BreakerAccumulator();
      }
    }

    // Opportunistically drain any buffered backlog while we have a live
    // connection, preserving each reading's original timestamp. Capped
    // per send-tick so a large backlog after a long outage can't starve
    // live sampling by hogging the loop.
    const uint16_t MAX_FLUSH_PER_TICK = 20;
    uint16_t flushed = 0;
    Reading buffered;
    while (mqttHandler.isConnected() && flushed < MAX_FLUSH_PER_TICK && ringBuffer.pop(buffered)) {
      if (!mqttHandler.publish(buffered)) {
        // Publish failed (e.g. broker transiently refused). Push it back
        // in - since this is a ring buffer, not a deque, it goes to the
        // back rather than staying at the front, which can reorder it
        // behind newer buffered entries. That's acceptable here because
        // every reading carries its own original `timestamp`, so
        // downstream trend reconstruction in Layer 4 uses that field,
        // not MQTT arrival order.
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
