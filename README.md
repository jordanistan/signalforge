# SignalForge

### Mesh intelligence for the airwaves around you.

SignalForge is a local-first operations console for a Meshtastic radio and the signals around it. It brings mesh nodes, direct messages, maps, weather, satellites, aircraft, and receive-only SDR tools into one browser dashboard, with a full terminal interface for working over SSH.

Meshtastic is the radio protocol and device ecosystem underneath. SignalForge is the product layer: the software that helps you see, understand, and act on the radio environment without sending anything until you explicitly choose to send a mesh message.

## What it does

SignalForge has one clear job: turn a small local radio setup into a useful field console.

- **Mesh operations** — inspect known nodes, positions, signal history, battery and route data; send direct or broadcast messages; track delivery state; keep per-node conversations.
- **Command Center** — see connection health, unread chats, follows, aircraft, SDR activity, and a live event stream without leaving the dashboard.
- **Maps** — use OpenStreetMap tiles when online or a bundled offline vector map when offline. Nodes without GPS still appear in a recent-heard list.
- **Airspace** — decode receive-only ADS-B with `rtl_adsb`, including callsign, altitude, speed, heading, vertical rate, and validated aircraft positions.
- **Spectrum** — survey six useful bands with `rtl_power`, inspect active channels, and listen to one analog narrow-FM channel at a time.
- **Satellites** — calculate current positions, ground tracks, and passes from validated CelesTrak orbital elements.
- **Weather** — show NWS observations, forecasts, alerts, radar, and nearby 433 MHz environmental sensors.
- **Follow mode** — let an allowed handheld request a topic such as `follow Austin traffic`; an external assistant can research due topics and queue concise updates back over the mesh.
- **Bridge mode** — connect an allowed remote node to a Claude session or another operator through a durable, byte-safe outbox.

SignalForge is receive-only for aircraft and SDR features. Mesh transmission happens only through an explicit browser, terminal, bridge, or follow response action.

The Air and Satellites map panels use SignalForge's bundled offline vector basemap. They do not depend on third-party map tiles, so a tile-provider policy response cannot blank those views.

## Start in demo mode

Demo mode needs no radio, SDR, internet feed, or saved node snapshot. It creates an isolated sample mesh state and labels outgoing messages as simulated.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tools/meshweb.py --demo
```

Open the private URL printed in the terminal. Demo files live under `run/demo/`, separate from live mesh history.

To connect a real radio:

```sh
.venv/bin/python -m pip install -r requirements-optional.txt
.venv/bin/python tools/meshweb.py --home 30.267,-97.743
```

`--home LAT,LON` is used when the radio has no GPS fix. Without it, SignalForge uses a clearly labelled assumed reference location. Use `--http-port`, `--state-dir`, `--no-radio`, `--no-weather`, and `--no-sats` to adapt the process to your setup.

The browser listens on loopback only and requires the token URL printed at startup. The token is stored with mode `0600` and reused across restarts; pass `--new-token` to rotate it.

## The interfaces

### Browser console

`tools/meshweb.py` owns the serial radio and serves the SignalForge web app. The default view is the Command Center; navigation also exposes Nodes, Chats, Air, Radio, Weather, Pagers, Sensors, and Satellites.

```sh
.venv/bin/python tools/meshweb.py --home 30.267,-97.743
.venv/bin/python tools/meshweb.py --bridge --allow '!02ecdcdc'
```

The browser protects the radio with a token cookie, strict Host/Origin checks, a restrictive Content Security Policy, text-only rendering for mesh content, and a 20-message-per-minute browser send limit. Only the visible chat clears unread state.

### Terminal console

Use the Textual console when you are on SSH or prefer a keyboard-first workflow:

```sh
.venv/bin/python tools/meshtui.py --home 30.267,-97.743
```

It includes Dashboard, Map, and Messages tabs. `F1` opens Dashboard, `F5` opens Messages, `F6` opens Map, `F4` changes theme, and `Ctrl+Q` exits. Click or press Enter on a node to open its direct-message thread.

### Headless bridge

The bridge prints messages from explicitly allowed node IDs and consumes replies queued by `mesh-reply.sh`:

```sh
.venv/bin/python tools/mesh-bridge.py --allow '!02ecdcdc'
MESH_OUTBOX=run/outbox tools/mesh-reply.sh 'Short update from the operator.'
```

Replies are split at word boundaries without exceeding 180 UTF-8 bytes. Sent chunks are checkpointed; failed chunks remain queued. The bridge does not perform research by itself: an external assistant or operator must decide what to send.

## SDR ownership

The SDR dongle is exclusive. SignalForge serializes survey, audio, ADS-B, pager, and sensor modes so two processes do not fight over the same hardware. Starting one mode releases or pauses another. `gqrx` and `tools/sdrscan.py` must not run at the same time as the browser Radio or Air panels.

Installed dependencies are reported per feature. A missing `rtl_433` disables Sensors without disabling mesh messaging; missing `pyModeS` disables Air without making the rest of the console fail.

| Feature | Python | System / external source |
| --- | --- | --- |
| Mesh + browser | `aiohttp`, `meshtastic` | Serial radio |
| Terminal UI | `textual` | Terminal |
| Spectrum survey | — | `rtl_power` |
| Analog listening | — | `rtl_fm`, `ffmpeg` |
| Aircraft | `pyModeS` | `rtl_adsb` |
| Pagers | — | `rtl_fm`, `multimon-ng` |
| Sensors | — | `rtl_433` |
| Satellites | `skyfield` | CelesTrak for fresh elements |
| Weather | `aiohttp` | `api.weather.gov` |

Run the read-only diagnostic any time:

```sh
make doctor
.venv/bin/python tools/doctor.py --json
```

## Project map

```text
tools/brand.py          product name, tagline, and User-Agent
tools/meshweb.py        SignalForge browser server and radio owner
tools/meshtui.py        terminal console
tools/meshcore.py       shared protocol, location, routing, and payload logic
tools/radiowatch.py     serialized SDR mode manager
tools/adsb.py           aircraft decoding and position validation
tools/decoders.py       pager and environmental sensor jobs
tools/weather.py        NWS client with section-level stale states
tools/satellites.py     validated TLE cache and pass calculations
tools/meshfollow.py     follow requests, expiry, budgets, and reports
tools/mesh-bridge.py    headless allowed-node bridge
web/                    SignalForge browser UI
data/basemap.json       offline vector map
data/tle/               orbital-element cache
run/                    local runtime state; never publish it
tests/                  fake-radio, service, browser, and CLI verification
```

## Development and verification

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
make verify
```

`make verify` runs Python, shell, and JavaScript syntax checks and the full test suite. Tests use fake radios, fake SDR processes, canned NWS responses, local browser fixtures, and temporary state directories. They do not transmit, contact live feeds, or flash firmware.

The current suite has **403 collected tests**. The known non-failing warning comes from Skyfield assigning a NumPy array dtype in a way NumPy 2.5 now deprecates.

## State, privacy, and recovery

Runtime files contain node IDs and positions, message history, follow topics, bearer URLs, and possibly exported radio configuration. Keep them private. `.gitignore` excludes runtime state, firmware downloads, backups, caches, and environment files.

- `run/messages.jsonl` — messages plus persisted delivery results.
- `run/nodes.json` — last node snapshot.
- `run/follows.json` — follow topics, expiry, checks, and message budgets.
- `run/outbox/` — pending bridge replies; temporary files are ignored.
- `run/web-url.txt` — private access URL.
- `data/tle/` — validated satellite cache.
- `backups/` — radio configuration exports, which may include private keys.

Malformed history rows are skipped so later messages remain readable. A DM acknowledgement means the radio reported delivery; broadcasts have no delivery receipt. A crash immediately after radio submission can repeat a queued bridge chunk, so the bridge is durable but not exactly-once.

## Hardware acceptance

Automated tests prove the software paths, not the antenna or USB radio. Before relying on a live deployment:

1. Run `make doctor` and confirm the intended serial path.
2. Start SignalForge and verify the radio identity and location source.
3. Send a short permitted DM and confirm a real acknowledgement.
4. Unplug and reconnect the radio; verify reconnect and queued-reply recovery.
5. Start and stop each installed SDR mode and confirm the dongle is released between modes.
6. Confirm live audio, ADS-B reception, weather refresh, and current satellite elements with the intended antenna and network.

Firmware upgrades remain a separate operation. See `tools/upgrade-heltec-v3.sh` and retain the configuration backup before flashing.

## Status

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for recovery details and [docs/STATUS.md](docs/STATUS.md) for the verification record and known limits.

SignalForge is intentionally a local operator tool. It binds to loopback; unattended deployment, remote authentication, and automatic web research are outside the current scope.
