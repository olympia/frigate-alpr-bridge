# frigate-alpr-bridge

Bridges Frigate license plate recognition events to the
[ALPR-Database](https://github.com/algertc/ALPR-Database) HTTP API.

Subscribes to the Frigate MQTT topic `frigate/tracked_object_update`,
filters for `type=lpr` payloads, debounces updates per event id, fetches
the snapshot and bounding box from the Frigate REST API, and POSTs a
Blue Iris / CodeProject AI compatible payload to ALPR-Database's
`/api/plate-reads` endpoint.

## What it does

1. Listens for `type=lpr` messages on `frigate/tracked_object_update`.
2. Buffers updates per event id. The latest update with the highest
   score wins. Each new update restarts the debounce timer.
3. After `DEBOUNCE_SECONDS` of inactivity for an event, fetches:
   - the snapshot JPEG from `/api/events/{id}/snapshot.jpg`
   - the event details from `/api/events/{id}` (for the license plate
     bounding box)
4. Synthesises a payload with a CodeProject AI ALPR style `ai_dump`
   array, base64-encoded `Image`, and BI-style fields (`camera`,
   `ALERT_PATH`, `ALERT_CLIP`, `timestamp`).
5. POSTs to ALPR-Database with the `x-api-key` header, retrying with
   exponential backoff on transient failures.

## Requirements

- Frigate 0.16 or newer with native LPR enabled (`lpr.enabled: true`).
- Frigate REST API reachable from the bridge container without
  authentication (or with auth disabled). If you need auth support, open
  an issue.
- An MQTT broker (typically the same one Frigate already publishes to).
- ALPR-Database running and accessible, with an API key generated in
  **Settings -> Security**.

## Frigate configuration

In your Frigate `config.yml`:

```yaml
lpr:
  enabled: true
```

Plus a camera configured for license plate detection, either using a
Frigate+ model with `license_plate` in `objects.track`, or the secondary
LPR pipeline with `type: "lpr"`. See the
[Frigate LPR documentation](https://docs.frigate.video/configuration/license_plate_recognition).

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MQTT_HOST` | yes | - | MQTT broker host or IP |
| `MQTT_PORT` | no | `1883` | MQTT broker port |
| `MQTT_USER` | no | empty | MQTT username (leave blank for anonymous) |
| `MQTT_PASS` | no | empty | MQTT password |
| `MQTT_TOPIC` | no | `frigate/tracked_object_update` | Topic to subscribe to |
| `MQTT_CLIENT_ID` | no | `frigate-alpr-bridge` | MQTT client id |
| `FRIGATE_URL` | yes | - | Frigate REST API base URL, e.g. `http://192.168.1.50:5000` |
| `ALPR_URL` | yes | - | ALPR-Database plate-reads URL, e.g. `http://192.168.1.60:3000/api/plate-reads` |
| `ALPR_API_KEY` | yes | - | ALPR-Database API key |
| `DEBOUNCE_SECONDS` | no | `3` | Debounce window per event id |
| `HTTP_TIMEOUT` | no | `10` | HTTP timeout (seconds) for Frigate and ALPR calls |
| `HTTP_RETRY_ATTEMPTS` | no | `3` | Number of POST attempts before giving up |
| `TZ` | no | unset | Container timezone (affects local timestamp formatting) |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

No values are baked into the image. All configuration is supplied at
container start.

## Installing on Unraid

1. Open the Docker tab and click **Add Container**.
2. Set **Template URL** to:
   ```
   https://raw.githubusercontent.com/olympia/frigate-alpr-bridge/main/unraid-template.xml
   ```
   and click the small load button next to the field. All variables
   appear pre-configured; fill in the required ones.
3. Alternatively configure manually:
   - **Repository**: `ghcr.io/olympia/frigate-alpr-bridge:latest`
   - **Network type**: choose a network from which the bridge can reach
     both the MQTT broker and the Frigate REST API. If Frigate is on a
     custom Docker network, place the bridge on the same one or use a
     network with routing between them.
   - Add the environment variables from the table above.
4. **Apply**. The container pulls the image and starts.
5. Watch the logs for `MQTT connected` and the first dispatched event.

## Verifying it works

Run a test plate recognition through Frigate. The container log should
show, for each event:

```
buffered new event id=... plate=ABC123 score=0.92
debounce elapsed, dispatching id=... plate=ABC123 score=0.95
ALPR DB accepted plate=ABC123 status=200
```

In ALPR-Database, check the **Live View** page; the plate should appear
within a few seconds of the Frigate event ending.

## Troubleshooting

**Bridge starts then immediately exits.** Check the log for `FATAL:
required environment variable ... is not set`. One of the required
variables is missing.

**MQTT connect failed.** Verify `MQTT_HOST`, `MQTT_PORT`, and
credentials. If the broker is on a different Docker network, make sure
this container can route to it.

**`snapshot fetch failed` or `event fetch failed`.** The bridge cannot
reach the Frigate REST API. Verify `FRIGATE_URL` is reachable from
inside the container (`docker exec` into it and `curl
"$FRIGATE_URL/api/version"`).

**`ALPR DB returned 401` or `403`.** Wrong `ALPR_API_KEY`. Re-copy from
ALPR-Database **Settings -> Security**.

**`ALPR DB returned 500`.** The ALPR-Database server rejected the
payload. Increase the log level to `DEBUG` and inspect the outgoing
payload. The most common cause is a missing or empty `Image` field, or
the ALPR-Database parser rejecting an `ai_dump` shape it does not
recognise. File an issue with a sanitised log excerpt.

**Plates land in ALPR-Database but with no image.** The snapshot fetch
failed silently. Check the logs for `snapshot fetch failed`. The bridge
sends an empty `Image` field rather than dropping the event.

**Same plate appears multiple times for one car.** Increase
`DEBOUNCE_SECONDS`. Frigate refines its recognition over time and emits
multiple updates per event; if `DEBOUNCE_SECONDS` is shorter than the
gaps between updates, each gap dispatches a separate POST.

## License

MIT
