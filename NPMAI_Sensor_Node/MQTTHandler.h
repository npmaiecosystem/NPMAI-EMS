#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include "Reading.h"
#include "config.h"

class MQTTHandler {
public:
  void begin();

  // Call every loop() iteration. Non-blocking-ish: retries connect at
  // most once per 5s if disconnected, otherwise services PubSubClient's
  // internal keepalive/incoming-packet handling.
  void update();

  bool isConnected();

  // Publishes immediately if connected. Returns false if not connected
  // or if the publish itself failed - caller should push to RingBuffer
  // in that case.
  bool publish(const Reading& r);

private:
  WiFiClient _wifiClient;
  PubSubClient _mqtt;
  unsigned long _lastReconnectAttemptMs = 0;

  bool connect();
  void buildTopic(const Reading& r, char* outTopic, size_t outTopicLen);
  size_t buildPayload(const Reading& r, char* outBuf, size_t outBufLen);
};
