#include "WiFiManager.h"

void WiFiManager::begin() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - start) < WIFI_CONNECT_TIMEOUT_MS) {
    delay(250);
  }
  _lastAttemptMs = millis();
}

void WiFiManager::update() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }
  unsigned long now = millis();
  if (now - _lastAttemptMs >= WIFI_RECONNECT_INTERVAL_MS) {
    _lastAttemptMs = now;
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD); // returns immediately; connection happens in the background
  }
}

bool WiFiManager::isConnected() const {
  return WiFi.status() == WL_CONNECTED;
}

int8_t WiFiManager::rssi() const {
  return isConnected() ? (int8_t)WiFi.RSSI() : 0;
}
