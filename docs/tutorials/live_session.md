# Running a live session with the glasses

Everything below was verified on a MacBook with Aria Gen 2 glasses and client SDK 2.5.1
(docs/ARCHITECTURE.md §0.7–0.11). Linux laptops should behave the same but have not been tried.

## 0. One-time setup

1. Install the SDK in the same environment as AriaStreamLab: `pip install 'ariastreamlab[aria]'`
   (`projectaria_client_sdk` + `projectaria_tools`).
2. Pair the laptop with the glasses over USB: `aria_gen2 auth pair`, accept in the Companion
   App, then `aria_gen2 auth check`. Pair over BLE as well if you want to start the hotspot
   without the cable: `aria_gen2 auth pair --ble --serial <serial>`.
3. `ariastreamlab doctor --device` — green for the SDK, the CLI, the streaming port and the pairing.

## 1. The experiment file

```json
{
  "extends": "preset:rgb_imu",
  "experiment": { "name": "pilot", "participant": "P001" },
  "streams": { "rgb": { "fps": 10 }, "imu": {}, "eye_gaze": {} },
  "device": {
    "connection": { "streaming_interface": "wifi_sap",
                    "hotspot": { "band": "5GHz", "bandwidth": "40MHz", "start": true } }
  }
}
```

`ariastreamlab inspect-config pilot.json` prints the *live plan*: profile, interface, batch
period and warnings such as "profile9 delivers about 5 Hz RGB" or "als has no live callback".
The default profile is `profile9`, the only one measured so far; `device.profile.mode:
"named"` uses any profile the device lists.

## 2. Run

```bash
ariastreamlab run pilot.json --duration 300
```

What happens, in order (each step is a `device_state` event in `events/events.parquet`):

1. **connect** over the USB network link (no adb needed) → `CONNECTED`;
2. **hotspot** — with `wifi_sap`, the adapter first asks the device for its hotspot status: if
   the hotspot is up and this laptop is already on it, nothing is touched (restarting a running
   hotspot bounces the access point and the laptop falls back to its home network — the device
   then reaches the laptop over the USB link only). Otherwise `aria_gen2 device hotspot start`
   runs (over USB, or BLE when `preferred_control` is `ble`), the SSID and password are printed,
   and the run waits for you to join that network and press ENTER (`hotspot.auto_join: true`
   does it with `networksetup` on macOS; `hotspot.start: false` skips the step entirely) → `NETWORK_CONFIGURED`;
3. **configure** — a streaming certificate is shared between the device and this laptop
   (`aria_gen2 streaming install-certs` runs only when the laptop has none yet: it generates
   a *new* certificate each time, so `device.connection.install_certs: "always"` forces a
   refresh and `"never"` skips it), our receiver starts on `0.0.0.0:6768` with that
   certificate, `HttpStreamingConfig` names it to the device → `STREAM_READY`;
4. **stream** → `STREAMING`. The glasses find the laptop as `oatmeal_server.local` and push
   data; the first samples arrive within a few seconds.
5. **control moves to Wi-Fi**: once data flows over the hotspot, the adapter opens a second
   control connection to the address the stream comes from and logs
   `control moved to Wi-Fi (…); the USB cable can be unplugged`. From then on status, quality
   statistics and the final stop go over Wi-Fi.
6. **stop** (duration elapsed or Ctrl-C) → `STOPPING` → `CONNECTED`; the receiver is released,
   which is what makes the SDK finalise the VRS file.

The `run` output shows per-stream rates, the pipeline line, and warnings; `--json` prints a
summary including `source.data_path` (`usb`/`wifi`), the stream source IP, SDK queue depths,
TicSync validity and raw-message counts.

## 3. What is written

```
sessions/<utc>_pilot_P001/
├── manifest.json        device serial/profile/interface/transport log, raw.files + sha256, pipeline stats, warnings
├── raw/<date-time>.vrs  the host-received VRS (the SDK names the file) — the ground truth
├── derived/…            processor outputs
├── events/events.parquet markers + device_state events
└── logs/session.log
```

Live raw streams are **not** duplicated as Parquet: the VRS is the raw record. To re-process it:

```bash
ariastreamlab replay sessions/<folder> --speed 0            # same config, every sample
ariastreamlab replay sessions/<folder>/raw/<file>.vrs --config other.json
```

## 4. Health while running

The adapter publishes on the bus every second (`health.link`: connections, data path, SDK
queue depths, TicSync), every 5 s (`health.device`: battery, charging, skin temperature,
thermal mitigation, Wi-Fi) and every 10 s (`health.quality`: the device's per-sensor
counters). Thermal mitigation starts at 42 °C skin temperature; a hotspot session warms the
glasses by ~7 °C in two minutes, so keep wireless sessions short (≈20 min) or tethered.

## 5. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `no Aria Gen 2 glasses found` | cable not in, or not paired: `aria_gen2 device list`, `aria_gen2 auth check` |
| `cannot start the streaming server on port 6768` | another receiver (`aria_streaming_viewer`, a previous run) holds the port: stop it or set `device.connection.server_port` |
| `no data received 20 s after start_streaming` | the laptop is not on the hotspot, a firewall blocks incoming 6768, the device cannot resolve `oatmeal_server.local`, or the certificate names differ (the SDK logs `Obtaining server cert name: cert_A` for our server and `Using preinstalled streaming certs: cert_B` for the device — they must be equal; run `aria_gen2 streaming install-certs` or set `install_certs: "always"` once) |
| data arrives but control never moves to Wi-Fi | port 23456 is not reachable on the stream's source address — keep the cable in; stop still works over USB |
| `start_streaming failed … already streaming` | a previous session was not stopped: `aria_gen2 streaming stop` |
| rates lower than nominal, device reports 0 drops | transport loss between the glasses and the laptop (visible as gaps in the VRS); move closer, use 5 GHz, or tether |
| eye gaze at 25 Hz instead of 30 | that is what the device delivers over this path (measured); not a bug in your configuration |
