#pragma once
#include <Adafruit_ADS1X15.h>   // needed here for the adsGain_t type used in MonitoringNode

// =====================================================================
// EDIT EVERYTHING IN THIS FILE FOR YOUR SITE. Nothing in the other
// .cpp/.h files should need to change per-installation.
// =====================================================================

// ---------------- WiFi ----------------
#define WIFI_SSID                  "YOUR_WIFI_SSID"
#define WIFI_PASSWORD               "YOUR_WIFI_PASSWORD"
#define WIFI_CONNECT_TIMEOUT_MS     15000   // bounded wait at boot only
#define WIFI_RECONNECT_INTERVAL_MS  10000   // background retry cadence once running

// ---------------- MQTT ----------------
#define MQTT_BROKER_HOST            "your-broker-host-or-ip"
#define MQTT_BROKER_PORT            1883
#define MQTT_USERNAME               ""      // leave "" for no-auth brokers
#define MQTT_PASSWORD               ""
#define MQTT_CLIENT_ID_PREFIX       "npmai-node-"
// PubSubClient's default internal buffer is only 256 bytes for the WHOLE
// packet (topic + payload + MQTT headers). Our topic strings plus JSON
// payload can exceed that, so we grow it explicitly in MQTTHandler.
#define MQTT_MAX_PAYLOAD_BYTES      512

// ---------------- NTP ----------------
#define NTP_SERVER_1                "pool.ntp.org"
#define NTP_SERVER_2                "time.nist.gov"
#define GMT_OFFSET_SEC               0       // set to your building's UTC offset in seconds
#define DAYLIGHT_OFFSET_SEC          0
#define NTP_SYNC_TIMEOUT_MS          15000

// ---------------- Device / building identity ----------------
#define BUILDING_ID   "bldg01"
#define PANEL_ID      "panel-2F-main"
#define DEVICE_ID     "esp32-2F-01"

// ---------------- I2C bus ----------------
#define I2C_SDA_PIN   21
#define I2C_SCL_PIN   22
#define I2C_CLOCK_HZ  400000
#define TCA9548A_ADDR 0x70

// ---------------- Local safety failsafe ----------------
#define FAILSAFE_GPIO_PIN        4      // drives buzzer/relay trip coil
#define FAILSAFE_RESET_BTN_PIN   5      // active-low, wired to GND via button, uses internal pullup
#define FAILSAFE_TEMP_C_DEFAULT  90.0f  // global default; override per-breaker below if needed
#define FAILSAFE_HYSTERESIS_C    5.0f   // must drop this far below threshold before auto-clearing
#define FAILSAFE_ACTIVE_HIGH     true   // set false if your relay/buzzer module is active-low

// ---------------- Sampling / send cadence ----------------
#define SAMPLE_INTERVAL_MS      10000   // how often raw sensors are read + failsafe evaluated
#define SEND_INTERVAL_MS        60000   // how often the SAMPLE_INTERVAL_MS averages are transmitted
#define CT_RMS_SAMPLE_COUNT     200     // ADC samples per RMS calc; see CurrentSensor.cpp for math
#define CT_OFFSET_FILTER_ALPHA  512.0   // adaptive DC-offset filter time constant; see CurrentSensor.cpp

// ---------------- Ring buffer (offline buffering) ----------------
#define RING_BUFFER_CAPACITY 360  // 360 readings x 60s send interval = 6 hrs bridged outage per breaker's queue slot

// ---------------- Node / breaker topology ----------------
// A "node" = one TCA9548A mux channel = one physical ADS1115 + one
// physical MLX90640 covering a group of breakers.
#define MAX_BREAKERS_PER_NODE 4
#define MLX90640_ROWS 24
#define MLX90640_COLS 32

struct BreakerZone {
  const char* breaker_id;
  uint8_t ads_channel;          // 0-3, which ADS1115 analog input this breaker's CT is wired to
  // Sub-rectangle of the 24(row) x 32(col) thermal frame that this
  // breaker's terminal/joint physically occupies in the camera's field
  // of view. MUST be measured during install (point the array at the
  // row, take a snapshot, identify which pixel block covers each lug).
  uint8_t thermal_row_start, thermal_row_end;
  uint8_t thermal_col_start, thermal_col_end;
  float failsafe_temp_c;        // per-breaker override in deg C; 0 = use FAILSAFE_TEMP_C_DEFAULT
};

struct MonitoringNode {
  uint8_t mux_channel;          // TCA9548A channel 0-7
  uint8_t ads1115_addr;         // typically 0x48 (ADDR pin to GND); see README for the other 3 options
  uint8_t mlx90640_addr;        // fixed at 0x33 on stock breakout boards
  adsGain_t ads_gain;           // GAIN_ONE = +/-4.096V full scale; pick based on your burden resistor sizing
  float ct_burden_ohms;         // burden resistor value, ohms
  float ct_ratio;               // CT turns ratio, e.g. 2000.0 for a 2000:1 CT
  // Reference zone used as "ambient" - pick pixels that see room air or
  // enclosure interior, NOT any breaker/bus bar, so it tracks ambient
  // drift rather than joint temperature.
  uint8_t ambient_row_start, ambient_row_end;
  uint8_t ambient_col_start, ambient_col_end;
  uint8_t num_breakers;
  BreakerZone breakers[MAX_BREAKERS_PER_NODE];
};

#define NUM_NODES 3

// EXAMPLE topology for a 12-breaker floor distribution board (3 mux
// channels x 4 breakers each). Replace thermal zone pixel ranges and CT
// calibration values with your actual measured install values.
static MonitoringNode NODES[NUM_NODES] = {
  // ---- Node 0: mux channel 0, breakers 1-4 ----
  {
    0, 0x48, 0x33, GAIN_ONE, 33.0f, 2000.0f,
    /*ambient zone*/ 0, 3, 0, 3,
    4,
    {
      {"brk-01", 0, 20, 23, 0,  7,  0},
      {"brk-02", 1, 20, 23, 8,  15, 0},
      {"brk-03", 2, 20, 23, 16, 23, 0},
      {"brk-04", 3, 20, 23, 24, 31, 0},
    }
  },
  // ---- Node 1: mux channel 1, breakers 5-8 ----
  {
    1, 0x48, 0x33, GAIN_ONE, 33.0f, 2000.0f,
    0, 3, 0, 3,
    4,
    {
      {"brk-05", 0, 20, 23, 0,  7,  0},
      {"brk-06", 1, 20, 23, 8,  15, 0},
      {"brk-07", 2, 20, 23, 16, 23, 0},
      {"brk-08", 3, 20, 23, 24, 31, 0},
    }
  },
  // ---- Node 2: mux channel 2, breakers 9-12 ----
  {
    2, 0x48, 0x33, GAIN_ONE, 33.0f, 2000.0f,
    0, 3, 0, 3,
    4,
    {
      {"brk-09", 0, 20, 23, 0,  7,  0},
      {"brk-10", 1, 20, 23, 8,  15, 0},
      {"brk-11", 2, 20, 23, 16, 23, 0},
      {"brk-12", 3, 20, 23, 24, 31, 0},
    }
  },
};
