#include "MQTTHandler.h"
#include <ArduinoJson.h>

void MQTTHandler::begin() {
  _mqtt.setClient(_wifiClient);
  _mqtt.setServer(MQTT_BROKER_HOST, MQTT_BROKER_PORT);
  // Must be called before connect(): PubSubClient's default internal
  // buffer is only 256 bytes for the ENTIRE packet (topic + payload +
  // MQTT fixed/variable headers). A topic like
  // "building/bldg01/panel/panel-2F-main/breaker/brk-01/telemetry"
  // plus our JSON payload can exceed that, causing silent publish
  // failures if left at the default.
  _mqtt.setBufferSize(MQTT_MAX_PAYLOAD_BYTES);
  _mqtt.setKeepAlive(30);
}

bool MQTTHandler::connect() {
  char clientId[48];
  snprintf(clientId, sizeof(clientId), "%s%s", MQTT_CLIENT_ID_PREFIX, DEVICE_ID);

  if (strlen(MQTT_USERNAME) > 0) {
    return _mqtt.connect(clientId, MQTT_USERNAME, MQTT_PASSWORD);
  }
  return _mqtt.connect(clientId);
}

void MQTTHandler::update() {
  if (WiFi.status() != WL_CONNECTED) {
    return; // no point attempting MQTT without a network link
  }
  if (!_mqtt.connected()) {
    unsigned long now = millis();
    if (now - _lastReconnectAttemptMs >= 5000) {
      _lastReconnectAttemptMs = now;
      connect();
    }
    return;
  }
  _mqtt.loop(); // services keepalive pings and incoming packets - must be called regularly
}

bool MQTTHandler::isConnected() {
  return _mqtt.connected();
}

void MQTTHandler::buildTopic(const Reading& r, char* outTopic, size_t outTopicLen) {
  // Topic structure: building/{building_id}/panel/{panel_id}/breaker/{breaker_id}/telemetry
  snprintf(outTopic, outTopicLen, "building/%s/panel/%s/breaker/%s/telemetry",
           BUILDING_ID, r.panel_id, r.breaker_id);
}

size_t MQTTHandler::buildPayload(const Reading& r, char* outBuf, size_t outBufLen) {
  // ArduinoJson v7: JsonDocument is a single dynamically-sized document
  // type (no more manually computing StaticJsonDocument<N> capacity like
  // in v6). It grows/shrinks its internal pool as needed.
  JsonDocument doc;
  doc["device_id"]  = r.device_id;
  doc["panel_id"]   = r.panel_id;
  doc["breaker_id"] = r.breaker_id;
  doc["timestamp"]  = r.timestamp;
  doc["I_rms"]      = r.I_rms;
  doc["T_hotspot"]  = r.T_hotspot;
  doc["T_ambient"]  = r.T_ambient;
  doc["wifi_rssi"]  = r.wifi_rssi;

  size_t len = serializeJson(doc, outBuf, outBufLen);
  return len; // 0 if it didn't fit in outBufLen
}

bool MQTTHandler::publish(const Reading& r) {
  if (!_mqtt.connected()) {
    return false;
  }

  char topic[96];
  buildTopic(r, topic, sizeof(topic));

  char payload[MQTT_MAX_PAYLOAD_BYTES];
  size_t len = buildPayload(r, payload, sizeof(payload));
  if (len == 0) {
    return false; // serialization failed or was truncated
  }

  return _mqtt.publish(topic, (const uint8_t*)payload, len, false);
}
