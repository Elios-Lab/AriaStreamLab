# Changelog

## 0.2.0 — 2026-10-22

The application window. `ariastreamlab` with no arguments opens a desktop window (pywebview:
WebKit on macOS, WebView2 on Windows, GTK/Qt on Linux) with Home, Design, Monitor and
Sessions pages; `ariastreamlab app --browser` serves the same application to a browser.

- Home page; Sessions page (list the recordings under `storage.root`, manifest facts,
  quality summary, events and intervals, replay at a chosen speed, open the folder).
- The monitor moved from `/` to `/monitor`; every page shares one navigation header.
- `dashboard.native` lets `run` and `replay` open a window instead of a browser tab.
- `api.serve_app`, `api.launch_replay`; pywebview is a core dependency, with a browser
  fallback when no WebView is available.
- Setup page: environment, packages, the `doctor` checks, and one-click install of Meta's
  SDK / `projectaria_tools`, pairing, streaming certificates, hotspot and stuck-stream
  commands with their output streamed into the page; `scripts/install.sh` / `install.ps1`
  create a supported Python environment and start the application.

## 0.1.1 — 2026-10-22

Lessons of the 20-minute validation session (hardware run 15, architecture §0.13): the
glasses reached 42 °C while charging over USB and ended the stream themselves after 640 s;
the framework kept running blind for ten minutes.

- Stall detection: `device.connection.stall_timeout_s` (30 s) and `on_stall` (`stop` by
  default): a `warning` event, then a clean end of the session with manifest and summary.
- Effective rates fall to 0 after one window without data on paced sources.
- Quality summary: per-stream coverage and outage list, the device's own sample counts
  against what the host received, streaming time apart from session time, rates judged
  against the live plan's expected rate; `wifi_connected` warns only on a true→false
  transition; plan notes and the control hand-over are notes, not warnings.
- "No active session to stop" is "already stopped", not an error.
- `profile9`: eye gaze and hand tracking are 30 Hz in steady state (25 Hz was a start-up
  transient); only RGB (5 Hz) remains a measured deviation.
- The statistics tap drains on close and `run()` waits for it (counts were occasionally
  short by a few samples right after a max-speed replay).
- NiceGUI is a core dependency: the dashboard is how a stall or a thermal shutdown is seen
  while it happens.
- Documentation revised for researchers (README, tutorials, architecture).

## 0.1.0 — 2026-10-22

First complete version: configuration (Pydantic, presets, JSON Schema, designer), core data
model and event bus, timing utilities, synthetic source, Aria Gen 2 adapter (verified on
hardware), crash-safe session storage with manifest provenance, session/VRS replay through
the same pipeline, processing graph with plugin processors, browser dashboard and experiment
designer, annotations (markers, intervals, config changes), quality monitoring and summary,
CLI, tutorials.
