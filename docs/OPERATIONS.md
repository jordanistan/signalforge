# SignalForge operations

## Dependencies and capabilities

Install the Python manifests from the repository root using `.venv/bin/python -m pip install -r FILE`.
`requirements.txt` contains core interfaces; `requirements-optional.txt` adds astronomy/ADS-B;
`requirements-dev.txt` includes both and the test tools. Direct dependencies are pinned to versions
verified in this workspace; this is not a complete transitive lockfile.

| Feature | Python dependency | System programs / service |
| --- | --- | --- |
| Browser interface and demo | aiohttp | Browser; local loopback socket |
| Mesh connection | meshtastic | Serial device access |
| Terminal interface | textual | Interactive terminal |
| SDR survey | Core | rtl_power |
| FM audio | Core | rtl_fm, ffmpeg |
| Aircraft | pyModeS | rtl_adsb |
| Pager decoder | Core | rtl_fm, multimon-ng |
| Environmental sensors | Core | rtl_433 |
| Satellite tracking | skyfield | CelesTrak for initial/updated orbital elements |
| Weather | aiohttp | api.weather.gov |
| Browser tests | playwright | Chromium; loopback sockets |
| Syntax checks | Python | bash, Node.js |

`make doctor` or `.venv/bin/python tools/doctor.py --json` checks installed dependencies and path
access without opening devices, transmitting, contacting external services, or exposing the token.
The UI reports capability errors per mode. Dependency availability does not prove a dongle is plugged in
or that it is free; busy/missing device errors appear when starting the selected mode.

## Launch and stop

```sh
.venv/bin/python tools/meshweb.py --home 30.267,-97.743
.venv/bin/python tools/meshweb.py --demo --http-port 8766
.venv/bin/python tools/meshweb.py --bridge --allow '!02ecdcdc'
.venv/bin/python tools/mesh-bridge.py --allow '!02ecdcdc'
```

Run only one live serial interface at a time. Stop with Ctrl+C; normal cleanup closes the serial radio,
browser connections, feed tasks, and any owned SDR processes. Unplugging a serial device leaves the
server running and reconnecting. If the configured device path disappears, automatic discovery is used
only when exactly one candidate radio exists.

If startup reports `Could not exclusively lock port`, another Meshtastic client already owns the radio.
Stop that client (including a second SignalForge terminal or bridge), reconnect the device, and let the
web console retry its connection.

SDR modes are mutually exclusive: survey, audio, aircraft, pager, and sensors. Switching modes releases
the previous process. Closing an older audio stream cannot stop a newer one. Stop buttons and unavailable
states reflect the selected mode and its dependencies. Pager/sensor traffic and decoder diagnostics stay
in bounded memory; radio survey summaries are saved in `run/radio-activity.json`.

Use `--no-radio --no-weather --no-sats` for mesh-only operation without optional network feeds.
The street-map layer and radar images still require internet when requested in the browser; choose
the offline vector map to use `data/basemap.json`.

## State and recovery

| Path | Contents |
| --- | --- |
| `run/messages.jsonl` | Mesh messages and delivery-result journal records |
| `run/nodes.json` | Last serial node table |
| `run/follows.json` | Follow requests, expiry, checks, and reported-message budget |
| `run/outbox/` | Pending bridge replies; `.tmp` files are not consumed |
| `run/web-url.txt` | Private token URL (mode 0600); reused on restart |
| `run/demo/` | Separate demo messages, nodes, follows, outbox, and token URL |
| `data/tle/` | Validated orbital-element cache |
| `backups/` | Exported radio configuration, potentially including private keys |

`--state-dir` moves the mesh runtime files. SDR survey output and the orbital cache retain the paths
listed above. The standalone scanner and TUI also retain their default runtime paths.
Use `--new-token` to rotate web access, then reopen the newly printed URL.

JSONL history skips malformed rows and continues reading later messages. Delivery journal records
restore delivered/failed status after restart; old histories without receipts remain readable.
An acknowledgement means the radio reported delivery. A submitted broadcast has no delivery receipt.

The bridge splits replies at UTF-8 byte boundaries and prefers word boundaries. After each chunk is
accepted by the serial send call, it replaces the queue file with the unsent remainder. Missing radios,
missing destinations, and send errors retain queued text. A queued reply goes to the first allowed node
when processed; inspect the queue before changing that destination.

Submission is not exactly-once delivery: a crash after transmission but before the queue checkpoint can
repeat a chunk, and a negative/lost acknowledgement does not automatically requeue an accepted send.
Check the message delivery state when this distinction matters. No background worker interprets bridge
message content as commands or automatically performs web research.

For custom state directories, queue explicitly:

```sh
MESH_OUTBOX=/path/to/state/outbox tools/mesh-reply.sh 'Short update'
MESH_ALLOW_SPLIT=1 MESH_OUTBOX=/path/to/state/outbox tools/mesh-reply.sh 'A longer update...'
```

## Feed freshness

Weather refreshes about every ten minutes; manual requests are limited to one every 30 seconds per server.
Forecasts, hourly data, alerts, and observations have independent current/stale/unavailable states.
An observation older than three hours is labelled stale. A failed alert fetch retains the last known list
with a warning; it never becomes a claim that no alerts exist. Location source is shown in the interface.

Satellite elements are refreshed when their cache is older than twelve hours, with retry throttling when
no cache is available. Invalid downloads cannot replace usable cached records. Cached calculations remain
available offline, with warnings for failed refreshes and old epochs. Pass predictions update for changes
in observer location; cached pass calculations are refreshed about every fifteen minutes. Downlink labels
are reference information and do not establish current transmitter availability.

## Hardware acceptance still required

Automated verification uses fake hardware and canned external data. With the intended devices connected:

1. Check `make doctor`, then start the live server and verify its selected local node and location.
2. Send a short permitted DM and channel message; observe a real acknowledgement for the DM.
3. Unplug/reconnect the serial radio and check recovery, including a reply queued while offline.
4. Start each installed SDR mode, switch modes, stop it, and confirm another receiver can acquire the dongle.
5. Test live audio, reception with the intended antenna, weather refresh, and current orbital-element downloads.

Firmware upgrades remain a separate explicit operation. Review the existing upgrade section in the README,
verify the exact board/image, and retain the configuration backup. Implementation/testing here never flashes hardware.

## Sharing the workspace

`.gitignore` excludes runtime data, configuration backups, firmware downloads, caches, and environment files.
Ignore rules do not remove files already tracked elsewhere. Review the actual file list before publishing:
node positions, messages, and exported configurations are personal data, and the token URL grants radio control.
The server intentionally binds to loopback; remote exposure/authentication and unattended service installation
are not implemented workflows.
