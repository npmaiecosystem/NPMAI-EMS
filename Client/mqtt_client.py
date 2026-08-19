import json
import logging

from gmqtt import Client as MQTTClient
from pydantic import ValidationError

from app.batch_writer import BatchWriter
from app.config import settings
from app.models import TelemetryPayload
from app.security import DeviceCache

logger = logging.getLogger(__name__)


class TelemetryMQTTSubscriber:
    """
    Subscribes to building/+/panel/+/breaker/+/telemetry using the
    ingestion service's own privileged MQTT identity (provisioned by
    scripts/create_ingestion_mqtt_user.py - a credential separate from
    any individual device's), and hands every valid message to the
    BatchWriter.

    Chose gmqtt over paho-mqtt for this service specifically because it's
    asyncio-native: on_message can be (and here, is) a coroutine that
    runs directly on FastAPI's own event loop, with no thread bridge
    needed to get back into async code to call the async BatchWriter/
    DeviceCache. paho-mqtt's callback model is synchronous and expects
    its network loop to own a thread, which would mean marshalling every
    message across a thread boundary to reach our async DB/Redis calls -
    solvable, but unnecessary complexity gmqtt avoids outright. (The test
    simulator in test/simulate_device.py uses paho-mqtt instead, since
    that's a simple synchronous script with no event loop of its own to
    integrate with - the right tool differs by context.)
    """

    def __init__(self, batch_writer: BatchWriter, device_cache: DeviceCache) -> None:
        self._batch_writer = batch_writer
        self._device_cache = device_cache
        self.client = MQTTClient(f"npmai-ingestion-{id(self):x}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self.client.set_auth_credentials(
            settings.mqtt_ingestion_username, settings.mqtt_ingestion_password
        )

    async def connect(self) -> None:
        await self.client.connect(settings.mqtt_host, settings.mqtt_port)

    async def disconnect(self) -> None:
        await self.client.disconnect()

    def _on_connect(self, client, flags, rc, properties):
        logger.info("Connected to MQTT broker, subscribing to %s", settings.mqtt_topic_filter)
        client.subscribe(settings.mqtt_topic_filter, qos=1)

    def _on_disconnect(self, client, packet, exc=None):
        logger.warning("Disconnected from MQTT broker (exc=%s) - gmqtt will auto-reconnect", exc)

    async def _on_message(self, client, topic: str, payload: bytes, qos: int, properties) -> int:
        try:
            await self._handle_message(topic, payload)
        except Exception:
            # This is the single most important line in this file: no
            # malformed packet, unexpected topic shape, or bug anywhere
            # in the handling below is allowed to propagate out of
            # on_message and kill the subscription. Per requirement #3:
            # log it and move on to the next message.
            logger.exception("Unhandled error processing MQTT message on topic %s", topic)
        return 0

    async def _handle_message(self, topic: str, payload: bytes) -> None:
        # Expected shape: building/{building_id}/panel/{panel_id}/breaker/{breaker_id}/telemetry
        # which splits into exactly 7 '/'-separated parts.
        parts = topic.split("/")
        if len(parts) != 7 or (parts[0], parts[2], parts[4], parts[6]) != ("building", "panel", "breaker", "telemetry"):
            logger.warning("Ignoring message on unexpected topic shape: %s", topic)
            return
        topic_panel_id, topic_breaker_id = parts[3], parts[5]

        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Malformed JSON on topic %s: %s", topic, exc)
            return

        try:
            reading = TelemetryPayload.model_validate(raw)
        except ValidationError as exc:
            logger.warning("Schema validation failed on topic %s: %s", topic, exc)
            return

        # ---- Defense in depth beyond broker-level auth ----
        # EMQX's built_in_database authenticator (docker-compose.yml)
        # already proved SOME valid, provisioned device credential made
        # this connection - anonymous/unauthenticated connections are
        # rejected before a message ever reaches us. What that alone does
        # NOT prove is that this specific credentialed connection is only
        # ever claiming ITS OWN device_id/panel_id/breaker_id: this pilot
        # does not yet enforce per-device MQTT topic ACLs (see README
        # "Known limitations"), so a compromised or misconfigured device
        # could technically publish under another breaker_id's topic.
        # We close that gap here at the application layer instead: verify
        # the topic and payload agree, and that the claimed device_id is
        # a real, active, registered device whose registered panel
        # matches the topic it published on.
        if topic_panel_id != reading.panel_id or topic_breaker_id != reading.breaker_id:
            logger.warning(
                "Topic/payload mismatch on %s: topic says panel=%s breaker=%s, payload says panel=%s breaker=%s",
                topic, topic_panel_id, topic_breaker_id, reading.panel_id, reading.breaker_id,
            )
            return

        device = self._device_cache.get_by_device_id(reading.device_id)
        if device is None or not device.is_active:
            logger.warning("Unknown or inactive device_id '%s' on topic %s, dropping", reading.device_id, topic)
            return
        if device.panel_id != reading.panel_id:
            logger.warning(
                "device_id '%s' claimed panel_id '%s' but is registered to '%s' - "
                "possible spoofing/misconfiguration, dropping",
                reading.device_id, reading.panel_id, device.panel_id,
            )
            return

        await self._batch_writer.enqueue(reading)  # failure already logged inside enqueue()