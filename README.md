# NPMAI-Predictive Sensor — Layer 1: ESP32 Edge Firmware

Pure acquisition + relay. No physics is computed here — that's Layers 3/4
in the cloud. This device's job: read CT current + IR thermal accurately,
get the data off-device reliably (buffering through outages), and
guarantee the local overtemp failsafe fires regardless of connectivity.

## File structure

```
NPMAI_Sensor_Node/
├── NPMAI_Sensor_Node.ino   Main sketch — setup()/loop(), ties everything together
├── config.h                 EVERYTHING you edit per-site: WiFi, MQTT, pins, node/breaker table
├── Reading.h                 Shared telemetry struct
├── I2CMux.h / .cpp            TCA9548A multiplexer channel switching
├── CurrentSensor.h / .cpp     ADS1115 CT clamp reading + true-RMS calculation
├── ThermalSensor.h / .cpp     MLX90640 frame reading + hotspot/ambient zone extraction
├── Failsafe.h / .cpp          Local, network-independent overtemp trip
├── WiFiManager.h / .cpp       Non-blocking connect/reconnect
├── TimeSync.h / .cpp          NTP-based real timestamps
├── RingBuffer.h / .cpp        In-RAM circular buffer for offline periods
└── MQTTHandler.h / .cpp       Topic building, JSON payload, publish, PubSubClient plumbing
```

This layout works as-is in **both** environments:

- **Arduino IDE**: open `NPMAI_Sensor_Node.ino` directly (the folder name
  must match the `.ino` filename, which it does). The IDE automatically
  compiles every `.cpp`/`.h` file sitting alongside it — no project file
  needed.
- **PlatformIO**: put this whole folder at `your_project/src/`. PlatformIO
  compiles a single `.ino` inside `src/` the same way Arduino IDE does.
  Minimal `platformio.ini`:

  ```ini
  [env:esp32dev]
  platform = espressif32
  board = esp32dev
  framework = arduino
  monitor_speed = 115200
  lib_deps =
      adafruit/Adafruit ADS1X15@^2.6.2
      adafruit/Adafruit MLX90640@^1.1.1
      adafruit/Adafruit BusIO
      knolleary/PubSubClient@^2.8
      bblanchon/ArduinoJson@^7.4.2
  ```
  (Swap `board = esp32dev` for your specific board if different.)

## Libraries to install — exact versions

Verified against each project's GitHub releases at time of writing.
Install via Arduino IDE → **Tools → Manage Libraries**, or the PlatformIO
`lib_deps` above.

| Library | Author | Version | Notes |
|---|---|---|---|
| **Adafruit ADS1X15** | Adafruit | **2.6.2** | ADS1115 ADC driver |
| **Adafruit MLX90640** | Adafruit | **1.1.1** | IR thermal array driver |
| **Adafruit BusIO** | Adafruit | latest | Dependency of both Adafruit libraries above — the Library Manager will pull it in automatically |
| **PubSubClient** | Nick O'Leary (knolleary) | **2.8** | MQTT client |
| **ArduinoJson** | Benoit Blanchon | **7.4.2** | JSON serialization — this code uses the v7 `JsonDocument` API, NOT the old v6 `StaticJsonDocument<N>` pattern, so don't install a 6.x version |
| ESP32 board package | Espressif Systems | any current 3.x | Install via Boards Manager (add `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json` as an Additional Board Manager URL if not already present). `WiFi.h`, `Wire.h`, `time.h`/`configTime()` are all part of this core and stable across recent versions. |

## Wiring / addressing

- **I2C bus**: SDA → GPIO 21, SCL → GPIO 22 (change in `config.h` if your
  board uses different pins). 400kHz clock.
- **TCA9548A mux** at default address `0x70` (all address pins to GND).
  Each of its 8 downstream channel pairs (SD0/SC0 … SD7/SC7) carries one
  ADS1115 + one MLX90640 for that node.
- **ADS1115** on each channel at address `0x48` (ADDR pin to GND). The
  chip supports 3 other addresses (`0x49`/`0x4A`/`0x4B` by tying ADDR to
  VDD/SDA/SCL respectively) if you ever need two ADS1115 boards behind
  one mux channel — not used in the example topology, which puts one
  ADS1115 (4 CT inputs) per mux channel.
- **MLX90640** on each channel at its fixed address `0x33` (not
  changeable on stock breakout boards — this is exactly why the mux is
  needed for more than one).
- **Failsafe output**: GPIO 4 → buzzer/relay trip input. **Reset button**:
  GPIO 5 → button → GND (internal pullup enabled in firmware).

## Calibration you MUST do per physical install

The example values in `config.h` are placeholders and will NOT be
correct for your hardware:

1. **CT ratio + burden resistor** (`ct_ratio`, `ct_burden_ohms` per
   node): read off your specific CT clamp's datasheet and the burden
   resistor you actually populated. Verify with a known load (e.g. a
   space heater of known wattage) and adjust `ct_ratio` until
   `I_rms` matches a clamp meter reading.
2. **Thermal zone pixel ranges** (`thermal_row_start/end`,
   `thermal_col_start/end` per breaker, `ambient_row/col_start/end` per
   node): point the installed MLX90640 at the breaker row, dump a frame
   over Serial (add a temporary debug print of `thermalFrame[]` as a
   24×32 grid), and identify which pixel block covers each breaker's
   terminal/lug and which block sees clear ambient air with no breaker
   in view.
3. **ADS1115 gain** (`ads_gain` per node): `GAIN_ONE` (±4.096V) is the
   default here — increase to `GAIN_FOUR` or `GAIN_EIGHT` if your CT +
   burden combination produces a smaller voltage swing, to use more of
   the ADC's dynamic range.

## Sizing / timing notes

- Default `CT_RMS_SAMPLE_COUNT = 200` at 860 SPS takes roughly
  200–400ms per breaker including I2C overhead. With 12 breakers across
  3 nodes, one full sample tick takes on the order of 3–5 seconds —
  comfortably inside the default 10s `SAMPLE_INTERVAL_MS`. If you scale
  up the breaker count, re-check this and raise `SAMPLE_INTERVAL_MS` if
  the sample tick starts running long.
- `RING_BUFFER_CAPACITY = 360` reservations per breaker's accumulator
  cycle ≈ 6 hours of bridged outage at the default 60s send interval.
  Raise it if your site sees longer outages and has free heap to spare
  (check with `ESP.getFreeHeap()`), or see the trade-off discussion in
  `RingBuffer.h` for switching to flash-backed buffering instead.

## What this firmware deliberately does NOT do

- No R_effective, EWMA, CUSUM, or any trend math — raw readings only.
  That's Layer 3/4's job, working from the raw rows this device writes
  via Layer 2's ingestion API.
- No AI/LLM calls, no severity classification — that's Layer 5.
- The failsafe threshold here is a single hardcoded trip point per
  breaker (with hysteresis), not the physics-aware Delta-T severity
  scale used upstream — it's intentionally dumb and independent, as the
  offline failsafe of last resort.
