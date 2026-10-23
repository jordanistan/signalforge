# Hardware, signals and antennas

SignalForge uses at most two devices: one Meshtastic radio on USB serial, and one RTL-SDR dongle.
Either can be missing; the features that need it are disabled and everything else keeps working.

Only the Meshtastic radio ever transmits, and only the text messages you send. The RTL-SDR is
receive-only by design (the hardware cannot transmit).

## Meshtastic radio

| Item | What SignalForge does |
| --- | --- |
| Connection | USB serial through the `meshtastic` Python library (`SerialInterface`). No Wi-Fi, Bluetooth or TCP connection is supported. |
| Default port | `/dev/ttyUSB0`. Change with `-p /dev/ttyACM0` or similar. |
| Reconnect | Unplugging leaves the server running and retrying. If the configured path disappears, it auto-discovers a radio only when exactly one candidate exists. |
| Configuration | Read-only. SignalForge never changes radio settings or channels. |
| Message size | About 200 bytes per radio message. Bridge replies are capped at 180 UTF-8 bytes per message. |

**Verified device:** Heltec WiFi LoRa 32 V3 (ESP32-S3, SX1262 LoRa) on `/dev/ttyUSB0`
through its CP210x USB-UART bridge. Other Meshtastic boards with a USB serial console should work the same
way (many show up as `/dev/ttyACM0`), but they have not been tested here.

Setup checklist:

1. Configure the radio's region, channels and name with the official Meshtastic apps or CLI first.
   SignalForge only uses what is already configured.
2. Attach the LoRa antenna before powering the board. Transmitting without an antenna can damage the radio.
3. Give your user serial access, then log out and back in:
   ```sh
   sudo usermod -aG uucp "$USER"      # Arch-based (this machine)
   sudo usermod -aG dialout "$USER"   # Debian / Ubuntu
   ```
4. Find the port and your node ID (`!` plus eight hex digits):
   ```sh
   ls /dev/ttyUSB* /dev/ttyACM*
   .venv/bin/python -m meshtastic --port /dev/ttyUSB0 --info | head -40
   ```
   Run the `--info` command only while SignalForge is stopped.

**One owner per serial port.** `meshweb.py`, `meshtui.py`, `mesh-bridge.py`, the Meshtastic CLI and
any other client cannot share the port. Check before starting one:

```sh
fuser -v /dev/ttyUSB0      # no output means the port is free
```

A busy port shows as `Could not exclusively lock port` or `Could not open radio`. See
[USER-GUIDE.md](USER-GUIDE.md#troubleshooting).

Firmware upgrades are outside SignalForge. `tools/upgrade-heltec-v3.sh` exists for the Heltec V3, but
only run it deliberately, with a configuration backup in `backups/`.

## RTL-SDR dongle

| Item | Detail |
| --- | --- |
| Supported | RTL2832U-based dongles, as used by the `rtl-sdr` tools. |
| Verified | USB ID `0bda:2838` ("RTL2838UHIDIR") with an R820T tuner. |
| Tuning range | 24 to 1766 MHz. The browser refuses frequencies outside it. |
| Gain | Fixed: 38 dB for survey and audio, 40 dB for ADS-B and sensors. |
| Kernel driver | If the DVB-T TV driver grabs the dongle, blacklist `dvb_usb_rtl28xxu`. Arch's `rtl-sdr` package ships this blacklist. |
| Permissions | The `rtl-sdr` package's udev rules let a normal user open the dongle. `No permission to open the SDR dongle` means those rules are missing. |

`make doctor` shows whether the dongle is plugged in (by USB ID), which process holds it, and warns
when the `dvb_usb_rtl28xxu` TV driver is loaded.

**One job at a time.** A single dongle can only tune one place. SignalForge runs exactly one of these
at once: band survey, audio listening, ADS-B, sensor decoding. Starting one stops the
previous job and releases the dongle first. The status chip on each SDR page shows what the dongle is
doing now. Receiver programs are started through `setpriv --pdeathsig` (util-linux) when available, so if
the server is killed they exit too and the dongle is released.

Outside programs also compete for the dongle. Do not run `gqrx`, `tools/sdrscan.py`, `rtl_test`, or
another `rtl_*` tool while an SDR page is active. The error `The SDR dongle is in use by another
program` means something else has it.

## What the SDR can see

Everything in this table is receive-only. Frequencies are the defaults and presets in
`tools/radiowatch.py`, `tools/decoders.py`, `web/page-radio.js` and `tools/satellites.py`.

| Signal | Frequency | Decoded by | Where it appears |
| --- | --- | --- | --- |
| Aircraft (ADS-B / Mode S) | 1090 MHz | best installed of `readsb`, `dump1090-fa`, `dump1090`, `rtl_adsb` (raw frames), then `pyModeS` | Air page, Command Center "Airspace" card, Map view |
| Band survey: 2 m amateur | 144 to 148 MHz | `rtl_power` (rtl-sdr) | Radio page, Command Center "Radio (SDR)" card |
| Band survey: MURS and NOAA weather | 151 to 163 MHz | `rtl_power` | Radio page |
| Band survey: 70 cm amateur | 440 to 450 MHz | `rtl_power` | Radio page |
| Band survey: UHF business / GMRS | 460 to 470 MHz | `rtl_power` | Radio page |
| Band survey: 800 MHz digital | 851 to 860 MHz | `rtl_power` (activity only; not decodable) | Radio page |
| Band survey: paging band (busy/quiet only) | 929 to 932 MHz | `rtl_power` | Radio page |
| Analog voice / audio | any 24 to 1766 MHz, narrow FM | `rtl_fm` (rtl-sdr) piped to `ffmpeg` (MP3 stream) | Radio page player |
| NOAA Weather Radio | 162.400 and 162.550 MHz presets | `rtl_fm` + `ffmpeg` | Radio page presets |
| ISS voice / SSTV downlink | 145.800 MHz | `rtl_fm` + `ffmpeg` (audio only) | Radio page preset; frequency shown on Satellites page |
| Environmental sensors | Auto (default): hops 433.92 / 315 / 915 MHz every 60 s; or one of 433.92, 315, 868.3, 915 MHz | `rtl_433` (JSON output) | Sensors page, Command Center Sensors card, Weather page "433 MHz" box |

Band survey labels known channels inside the sweeps: NOAA Weather Radio (162.39 to 162.56), the
146.94 2 m repeater, MURS, GMRS/FRS and the 929 to 932 pager channels. Survey summaries are saved in
`radio-activity.json` in the state directory (`run/` by default).

Things the SDR side does **not** do (do not expect them):

- No AM or SSB demodulation. The listen feature is narrow FM only, so aircraft voice (118 to 137 MHz AM)
  and shortwave are not usable.
- No digital voice. P25, DMR and trunked systems (the 800 MHz band) are shown as survey activity only.
- No weather-satellite images. NOAA APT and Meteor LRPT frequencies are listed for reference, but nothing
  decodes pictures. NOAA's older POES satellites (NOAA 15, 18, 19) are being retired, so some may no
  longer transmit even when a pass is shown.
- No transmitting of any kind.

Privacy filters built into the decoders:

- **No pager decoding.** There is deliberately no pager decoder: in the US, reading other people's display-pager messages is generally prohibited (ECPA), and they often carry medical details. The band survey only shows whether paging channels are busy.
- **Sensors:** only environmental readings (temperature, humidity, wind, rain, pressure, light, UV, soil
  moisture, lightning) are listed. Tyre-pressure (TPMS) sensors, car keys, remotes, door/window
  contacts, alarms and motion sensors are counted as "ignored" and their content is dropped, because
  they can be used to track vehicles or reveal what a household is doing.

Laws on receiving and using radio content vary by country. Check yours.

## Software per signal

`make doctor` reports which of these are present. A missing program only disables its own feature.

| Feature | Python package (in `requirements*.txt`) | System programs |
| --- | --- | --- |
| Browser, mesh | `aiohttp`, `meshtastic` | serial device access |
| Terminal UI | `textual` | none |
| Band survey | none | `rtl_power` |
| Listening | none | `rtl_fm`, `ffmpeg` |
| Aircraft | `pyModeS` | one of `readsb`, `dump1090-fa`, `dump1090`, `rtl_adsb` (first found wins; `stdbuf` from coreutils is used when present) |
| Sensors | none | `rtl_433` |
| Satellites | `skyfield` | none (CelesTrak download, then cached) |
| Weather | `aiohttp` | none (api.weather.gov) |

Install the system programs:

```sh
# Arch / Omarchy (package names verified in the official "extra" repo)
sudo pacman -S rtl-sdr ffmpeg rtl_433

# Debian / Ubuntu (typical names, not verified here)
sudo apt install rtl-sdr ffmpeg rtl-433
```

The default aircraft receiver is `rtl_adsb` (part of `rtl-sdr`) with `pyModeS`; this is the path that
has been used on hardware here. `rtl_adsb` is a weak demodulator and prints many noise frames. If
`readsb`, `dump1090-fa` or `dump1090` is installed, SignalForge uses it automatically instead (in that
order). Those are not in Arch's official repositories (AUR only), and that path has **not been tested
on hardware**.

ADS-B frames are validated before use: only DF17/18 extended squitters with a good CRC (a single-bit
error is repaired only for an aircraft heard intact in the last 120 seconds), and positions only from even/odd frame pairs, within 250 NM, and at plausible speeds.

## Antennas

Antenna choice matters more than any setting. These are general guidelines, not results measured with
SignalForge (except where noted). A quarter-wave element length is roughly 75 / MHz metres.

| Band | Use | Guidance |
| --- | --- | --- |
| 902 to 928 MHz (US LoRa) | Meshtastic radio | Use an antenna tuned for your region's LoRa band. Height and line of sight matter most. |
| 1090 MHz | ADS-B | A 1090 MHz antenna (quarter-wave about 6.9 cm), mounted high with a clear sky view, ideally outdoors or at a window. A stock whip indoors may hear only a few aircraft. |
| 144 to 174 MHz | 2 m, MURS, NOAA weather, VHF pagers, ISS | The telescopic dipole that ships with many dongles, extended to about 50 cm per side, works for strong local signals. |
| 420 to 470 MHz | 70 cm, GMRS/FRS, 433 MHz sensors | Same dipole, shortened to about 17 cm per side. |

Measured on this setup: a fixed 40 dB gain received far more valid ADS-B frames than automatic gain
(31 versus 1 message in 35 seconds), which is why ADS-B uses 40 dB.

Keep the SDR antenna away from the Meshtastic radio's antenna. The LoRa transmitter can overload a
nearby receiver when it sends.
