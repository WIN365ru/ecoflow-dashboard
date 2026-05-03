from __future__ import annotations

import json
import logging
import random
import ssl
import threading
import time as _time
import uuid

import paho.mqtt.client as mqtt

from .api import MqttCredentials
from .config import AUTH_PRIVATE

log = logging.getLogger(__name__)


def _flatten(obj: dict | list, prefix: str = "") -> dict[str, object]:
    """Flatten nested dict/list into dot-path keys."""
    items: dict[str, object] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                items.update(_flatten(v, key))
            else:
                items[key] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}.{i}"
            if isinstance(v, (dict, list)):
                items.update(_flatten(v, key))
            else:
                items[key] = v
    return items


class EcoFlowMqttClient:
    def __init__(self, credentials: MqttCredentials, device_sns: list[str], auth_mode: str = "public") -> None:
        self._creds = credentials
        self._device_sns = device_sns
        self._auth_mode = auth_mode
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, object]] = {sn: {} for sn in device_sns}
        self._last_update: dict[str, float] = {sn: 0.0 for sn in device_sns}
        self._connected = False
        # Map topic → SN for fast lookup
        self._topic_sn: dict[str, str] = {}

        if auth_mode == AUTH_PRIVATE:
            uid = uuid.uuid4().hex[:8]
            client_id = f"ANDROID_{uid}_{credentials.user_id}"
        else:
            client_id = f"ecoflow-dash-{uuid.uuid4().hex[:8]}"
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        self._client.username_pw_set(credentials.username, credentials.password)
        self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        # Keepalive thread to re-request state if data goes stale
        self._stop_keepalive = threading.Event()
        self._keepalive_thread: threading.Thread | None = None

    def start(self) -> None:
        log.info("Connecting to MQTT %s:%d", self._creds.host, self._creds.port)
        self._client.connect(self._creds.host, self._creds.port, keepalive=15)
        self._client.loop_start()
        # Start keepalive for Private API (re-requests stale data)
        if self._auth_mode == AUTH_PRIVATE:
            self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
            self._keepalive_thread.start()

    def _keepalive_loop(self) -> None:
        """Periodically re-request device state if MQTT data is stale.
        EcoFlow's broker sometimes silently stops pushing data — this
        keeps it flowing by sending a 'get' request every 2 minutes
        for any device that hasn't received an update in 90s.
        """
        while not self._stop_keepalive.is_set():
            self._stop_keepalive.wait(120)
            if self._stop_keepalive.is_set() or not self._connected:
                continue
            for sn in self._device_sns:
                age = self.last_update_age(sn)
                if age == float("inf") or age > 90:
                    try:
                        self._request_device_state(self._client, sn)
                    except Exception as e:
                        log.warning("Keepalive request failed for %s: %s", sn, e)

    def stop(self) -> None:
        self._stop_keepalive.set()
        self._client.loop_stop()
        self._client.disconnect()

    @property
    def connected(self) -> bool:
        return self._connected

    def send_command(self, sn: str, params: dict) -> None:
        """Publish a set command to a device via MQTT."""
        if self._auth_mode == AUTH_PRIVATE:
            topic = f"/app/{self._creds.user_id}/{sn}/thing/property/set"
        else:
            topic = f"/open/{self._creds.username}/{sn}/set"
        msg = {
            "from": "ecoflow-dash",
            "id": str(random.randint(100000, 999999)),
            "version": "1.0",
            "moduleType": 0,
            "operateType": "TCP",
            "params": params,
        }
        self._client.publish(topic, json.dumps(msg), qos=1)
        log.info("Sent command to %s: %s", sn, params)

    def get_device_data(self, sn: str) -> dict[str, object]:
        with self._lock:
            return dict(self._data.get(sn, {}))

    def set_initial_data(self, sn: str, data: dict) -> None:
        flat = _flatten(data)
        with self._lock:
            self._data.setdefault(sn, {}).update(flat)

    def _on_connect(self, client: mqtt.Client, userdata: object, flags: mqtt.ConnectFlags, rc: mqtt.ReasonCode, properties: mqtt.Properties | None = None) -> None:
        if rc != 0:
            log.error("MQTT connect failed: rc=%s", rc)
            return
        self._connected = True
        log.info("MQTT connected")

        for sn in self._device_sns:
            if self._auth_mode == AUTH_PRIVATE:
                topics = [
                    f"/app/device/property/{sn}",
                    f"/app/{self._creds.user_id}/{sn}/thing/property/set",
                    f"/app/{self._creds.user_id}/{sn}/thing/property/get",
                ]
            else:
                topics = [f"/open/{self._creds.username}/{sn}/quota"]

            for topic in topics:
                self._topic_sn[topic] = sn
                client.subscribe(topic, qos=1)
                log.info("Subscribed to %s", topic)

        # For private API, request current device state
        if self._auth_mode == AUTH_PRIVATE:
            for sn in self._device_sns:
                self._request_device_state(client, sn)

    def _request_device_state(self, client: mqtt.Client, sn: str) -> None:
        """Publish a 'get all properties' message to trigger device data push."""
        topic = f"/app/{self._creds.user_id}/{sn}/thing/property/get"
        msg = {
            "from": "Android",
            "id": str(random.randint(100000, 999999)),
            "version": "1.0",
        }
        client.publish(topic, json.dumps(msg), qos=1)
        log.info("Requested state for %s on %s", sn, topic)

    def _on_message(self, client: mqtt.Client, userdata: object, msg: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("Failed to decode MQTT message on %s", msg.topic)
            return

        # Look up SN from our topic→SN map
        sn = self._topic_sn.get(msg.topic)
        if not sn:
            # Try matching by checking if any known SN appears in the topic
            for known_sn in self._device_sns:
                if known_sn in msg.topic:
                    sn = known_sn
                    self._topic_sn[msg.topic] = sn
                    break
        if sn not in self._data:
            log.debug("Ignoring message on unknown topic: %s", msg.topic)
            return

        # The payload may be wrapped in "params" or be at top level
        data = payload.get("params", payload)
        flat = _flatten(data)

        log.debug("MQTT [%s] topic=%s fields=%d: %s", sn, msg.topic, len(flat),
                  list(flat.keys())[:10])

        with self._lock:
            self._data[sn].update(flat)
            self._last_update[sn] = _time.time()

    def last_update_age(self, sn: str) -> float:
        """Seconds since last MQTT message for this device."""
        ts = self._last_update.get(sn, 0.0)
        return _time.time() - ts if ts > 0 else float("inf")

    def _on_disconnect(self, client: mqtt.Client, userdata: object, flags: mqtt.DisconnectFlags, rc: mqtt.ReasonCode, properties: mqtt.Properties | None = None) -> None:
        self._connected = False
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%s), reconnecting...", rc)
        else:
            log.info("MQTT disconnected")
