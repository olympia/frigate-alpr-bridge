#!/usr/bin/env python3
"""
Frigate to ALPR-Database bridge.

Subscribes to the Frigate MQTT topic ``frigate/tracked_object_update``,
filters for ``type=lpr`` payloads, debounces updates per event id,
fetches the snapshot and event details from the Frigate REST API,
synthesises a Blue Iris / CodeProject AI compatible payload, and POSTs
it to the ALPR-Database HTTP API.

Configuration is read entirely from environment variables. No values
are baked into the image.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
import requests


# --- Configuration ----------------------------------------------------------

def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.stderr.write(
            f"FATAL: required environment variable {name} is not set\n"
        )
        sys.exit(2)
    return value


def _optional_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


MQTT_HOST = _required_env("MQTT_HOST")
MQTT_PORT = int(_optional_env("MQTT_PORT", "1883"))
MQTT_USER = _optional_env("MQTT_USER", "")
MQTT_PASS = _optional_env("MQTT_PASS", "")
MQTT_TOPIC = _optional_env("MQTT_TOPIC", "frigate/tracked_object_update")
MQTT_CLIENT_ID = _optional_env("MQTT_CLIENT_ID", "frigate-alpr-bridge")

FRIGATE_URL = _required_env("FRIGATE_URL").rstrip("/")
ALPR_URL = _required_env("ALPR_URL")
ALPR_API_KEY = _required_env("ALPR_API_KEY")

DEBOUNCE_SECONDS = float(_optional_env("DEBOUNCE_SECONDS", "3"))
HTTP_TIMEOUT = float(_optional_env("HTTP_TIMEOUT", "10"))
HTTP_RETRY_ATTEMPTS = int(_optional_env("HTTP_RETRY_ATTEMPTS", "3"))

LOG_LEVEL = _optional_env("LOG_LEVEL", "INFO").upper()


# --- Logging ----------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bridge")


# --- Debounce buffer --------------------------------------------------------

@dataclass
class PendingEvent:
    """An LPR event waiting for its debounce timer to elapse."""

    event_id: str
    payload: dict[str, Any]
    timer: threading.Timer | None = field(default=None, repr=False)


class DebounceBuffer:
    """
    Per-event_id debounce buffer.

    When a new LPR update arrives for an event_id, it replaces the stored
    payload only if its score is at least as high as the current best,
    and the debounce timer is restarted from zero. When the timer
    elapses, the final payload is dispatched.
    """

    def __init__(self, debounce_seconds: float, dispatch):
        self._debounce_seconds = debounce_seconds
        self._dispatch = dispatch
        self._lock = threading.Lock()
        self._pending: dict[str, PendingEvent] = {}

    def submit(self, payload: dict[str, Any]) -> None:
        event_id = payload.get("id")
        if not event_id:
            log.warning("LPR update without id, ignoring: %s", payload)
            return

        new_score = float(payload.get("score") or 0.0)

        with self._lock:
            existing = self._pending.get(event_id)
            if existing is None:
                pending = PendingEvent(event_id=event_id, payload=payload)
                self._pending[event_id] = pending
                log.info(
                    "buffered new event id=%s plate=%s score=%.2f",
                    event_id,
                    payload.get("plate"),
                    new_score,
                )
            else:
                existing_score = float(existing.payload.get("score") or 0.0)
                if new_score >= existing_score:
                    existing.payload = payload
                    log.debug(
                        "updated event id=%s plate=%s score=%.2f -> %.2f",
                        event_id,
                        payload.get("plate"),
                        existing_score,
                        new_score,
                    )
                else:
                    log.debug(
                        "ignored lower-score update id=%s score=%.2f < %.2f",
                        event_id,
                        new_score,
                        existing_score,
                    )
                if existing.timer is not None:
                    existing.timer.cancel()
                pending = existing

            timer = threading.Timer(
                self._debounce_seconds, self._on_timer_elapsed, args=(event_id,)
            )
            timer.daemon = True
            pending.timer = timer
            timer.start()

    def _on_timer_elapsed(self, event_id: str) -> None:
        with self._lock:
            pending = self._pending.pop(event_id, None)
        if pending is None:
            return
        log.info(
            "debounce elapsed, dispatching id=%s plate=%s score=%.2f",
            event_id,
            pending.payload.get("plate"),
            float(pending.payload.get("score") or 0.0),
        )
        try:
            self._dispatch(pending.payload)
        except Exception:
            log.exception("dispatch failed for id=%s", event_id)


# --- Frigate REST API client ------------------------------------------------

class FrigateClient:
    """Minimal Frigate REST API client. Assumes auth is disabled."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url
        self._timeout = timeout
        self._session = requests.Session()

    def get_snapshot(self, event_id: str) -> bytes | None:
        """Download the snapshot JPEG for an event. Returns bytes or None."""
        url = f"{self._base_url}/api/events/{event_id}/snapshot.jpg"
        try:
            response = self._session.get(url, timeout=self._timeout)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            log.warning("snapshot fetch failed id=%s: %s", event_id, exc)
            return None

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Fetch the full event payload. Returns dict or None."""
        url = f"{self._base_url}/api/events/{event_id}"
        try:
            response = self._session.get(url, timeout=self._timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            log.warning("event fetch failed id=%s: %s", event_id, exc)
            return None
        except ValueError as exc:
            log.warning("event JSON parse failed id=%s: %s", event_id, exc)
            return None


# --- ALPR-Database HTTP client ----------------------------------------------

class AlprDatabaseClient:
    """Posts plate reads to the ALPR-Database HTTP API."""

    def __init__(
        self,
        url: str,
        api_key: str,
        timeout: float,
        retry_attempts: int,
    ):
        self._url = url
        self._api_key = api_key
        self._timeout = timeout
        self._retry_attempts = max(1, retry_attempts)
        self._session = requests.Session()

    def post(self, payload: dict[str, Any]) -> bool:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
        }
        for attempt in range(1, self._retry_attempts + 1):
            try:
                response = self._session.post(
                    self._url,
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=self._timeout,
                )
                if 200 <= response.status_code < 300:
                    log.info(
                        "ALPR DB accepted plate=%s status=%d",
                        payload.get("ai_dump", [{}])[0]
                        .get("found", {})
                        .get("predictions", [{}])[0]
                        .get("plate"),
                        response.status_code,
                    )
                    return True
                log.warning(
                    "ALPR DB returned %d body=%s (attempt %d/%d)",
                    response.status_code,
                    response.text[:300],
                    attempt,
                    self._retry_attempts,
                )
            except requests.RequestException as exc:
                log.warning(
                    "ALPR DB POST error: %s (attempt %d/%d)",
                    exc,
                    attempt,
                    self._retry_attempts,
                )
            if attempt < self._retry_attempts:
                time.sleep(2 ** (attempt - 1))
        log.error("ALPR DB POST failed after %d attempts", self._retry_attempts)
        return False


# --- Payload synthesis ------------------------------------------------------

def _extract_license_plate_box(event: dict[str, Any] | None) -> list[int] | None:
    """
    Pull the license_plate bounding box from a Frigate event payload.

    The structure has a ``data.attributes`` array, each entry being
    ``{"label": "license_plate", "score": 0.x, "box": [x_min, y_min, x_max, y_max]}``.
    Picks the highest-scoring license_plate attribute.
    """
    if not event:
        return None
    data = event.get("data") or {}
    attributes = data.get("attributes") or []
    plates = [a for a in attributes if a.get("label") == "license_plate"]
    if not plates:
        return None
    best = max(plates, key=lambda a: float(a.get("score") or 0.0))
    box = best.get("box")
    if isinstance(box, list) and len(box) == 4:
        return [int(v) for v in box]
    return None


def _build_ai_dump(
    payload: dict[str, Any],
    box: list[int] | None,
) -> list[dict[str, Any]]:
    """Synthesise a CodeProject AI ALPR-style ai_dump payload."""
    plate = payload.get("plate") or ""
    score = float(payload.get("score") or 0.0)

    prediction: dict[str, Any] = {
        "confidence": score,
        "label": f"Plate: {plate}",
        "plate": plate,
    }
    if box:
        prediction["x_min"] = box[0]
        prediction["y_min"] = box[1]
        prediction["x_max"] = box[2]
        prediction["y_max"] = box[3]

    return [
        {
            "api": "alpr",
            "found": {
                "success": True,
                "processMs": 0,
                "inferenceMs": 0,
                "predictions": [prediction],
                "message": f"Found Plate: {plate}",
                "moduleId": "ALPR",
                "moduleName": "Frigate LPR Bridge",
                "code": 200,
                "command": "alpr",
                "inferenceDevice": "Frigate",
                "processedBy": "frigate-alpr-bridge",
                "timestampUTC": datetime.now(timezone.utc).strftime(
                    "%a %d %b %Y %H:%M:%S GMT"
                ),
            },
        }
    ]


def _format_timestamp(epoch: float | None) -> str:
    """Format a Frigate epoch timestamp as a Blue Iris-style local string."""
    if epoch is None:
        epoch = time.time()
    return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")


# --- Dispatcher -------------------------------------------------------------

class Dispatcher:
    """Glues the Frigate client and the ALPR DB client together."""

    def __init__(self, frigate: FrigateClient, alpr: AlprDatabaseClient):
        self._frigate = frigate
        self._alpr = alpr

    def __call__(self, payload: dict[str, Any]) -> None:
        event_id = payload.get("id")
        plate = payload.get("plate") or ""
        camera = payload.get("camera") or ""

        if not event_id or not plate:
            log.warning("missing id or plate, skipping: %s", payload)
            return

        snapshot_bytes = self._frigate.get_snapshot(event_id)
        if snapshot_bytes is None:
            log.warning("no snapshot for id=%s, sending without image", event_id)
            image_b64 = ""
        else:
            image_b64 = base64.b64encode(snapshot_bytes).decode("ascii")

        event = self._frigate.get_event(event_id)
        box = _extract_license_plate_box(event)
        if box is None:
            log.debug("no license_plate bbox for id=%s", event_id)

        ai_dump = _build_ai_dump(payload, box)
        timestamp = _format_timestamp(payload.get("timestamp"))

        outgoing = {
            "ai_dump": ai_dump,
            "Image": image_b64,
            "camera": camera,
            "ALERT_PATH": "",
            "ALERT_CLIP": "",
            "timestamp": timestamp,
        }

        self._alpr.post(outgoing)


# --- MQTT plumbing ----------------------------------------------------------

class MqttRunner:
    """Owns the MQTT client and forwards LPR updates to the buffer."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        topic: str,
        client_id: str,
        buffer: DebounceBuffer,
    ):
        self._host = host
        self._port = port
        self._topic = topic
        self._buffer = buffer

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id
        )
        if user:
            self._client.username_pw_set(user, password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info(
                "MQTT connected to %s:%d, subscribing to %s",
                self._host,
                self._port,
                self._topic,
            )
            client.subscribe(self._topic)
        else:
            log.error("MQTT connect failed reason=%s", reason_code)

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ):
        log.warning("MQTT disconnected reason=%s", reason_code)

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("malformed MQTT payload: %s", exc)
            return

        if payload.get("type") != "lpr":
            return

        log.debug("received LPR update: %s", payload)
        self._buffer.submit(payload)

    def run(self) -> None:
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_forever(retry_first_connection=True)

    def stop(self) -> None:
        self._client.disconnect()


# --- Entry point ------------------------------------------------------------

def main() -> int:
    log.info(
        "starting frigate-alpr-bridge: frigate=%s alpr=%s topic=%s debounce=%.1fs",
        FRIGATE_URL,
        ALPR_URL,
        MQTT_TOPIC,
        DEBOUNCE_SECONDS,
    )

    frigate = FrigateClient(FRIGATE_URL, timeout=HTTP_TIMEOUT)
    alpr = AlprDatabaseClient(
        ALPR_URL, ALPR_API_KEY, timeout=HTTP_TIMEOUT, retry_attempts=HTTP_RETRY_ATTEMPTS
    )
    dispatcher = Dispatcher(frigate, alpr)
    buffer = DebounceBuffer(DEBOUNCE_SECONDS, dispatcher)

    runner = MqttRunner(
        MQTT_HOST,
        MQTT_PORT,
        MQTT_USER,
        MQTT_PASS,
        MQTT_TOPIC,
        MQTT_CLIENT_ID,
        buffer,
    )

    def _shutdown(signum, frame):
        log.info("signal %s received, shutting down", signum)
        runner.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        runner.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
