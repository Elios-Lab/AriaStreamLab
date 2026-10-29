# Changelog

## Unreleased

- Setup page, from the first hardware validation of 0.2: text can be selected and copied in
  the native window; the Output card wraps long lines and has *copy* and *save* buttons; the
  checks keep their detail beside the name; a conda environment is named as such; the
  `device list` check says what is reachable and what is paired instead of echoing the CLI's
  last line; the install buttons use `uv pip` in an environment that has no pip module.
- `docs/manual.md`: setting up a data-collection project, from the research question to
  analysed sessions, with three worked projects in `examples/projects/` (gaze on fixation
  targets, visual search on a desk, walking laps), each with its experiment file, protocol
  script, session-day checklist and analysis script, all runnable on synthetic data.
- `Experiment.start()` / `wait()` / `running`: acquisition on a background thread so that a
  protocol script drives the annotations from the main thread; `from_json(participant=…)`
  and `ariastreamlab run --participant` override the file's participant id.
- Quality summary: the mock's configured `fps` / `rate_hz` are the expected rates (no false
  "below 80 %" warning for `fps: 5`); no coverage for one-shot streams (`device_calibration`)
  or for derived streams that emit on changes.

## 0.2.1 — 2026-10-28

Continuous integration and the first GitHub release; no change to the application or the
recorded sessions.

- GitHub Actions: ruff, mypy and the import contracts; the tests on Ubuntu and macOS with
  Python 3.10–3.12 (hardware tests deselected); a wheel build and install check.
- A release workflow: pushing a `vX.Y.Z` tag builds the sdist and wheel and publishes the
  GitHub release with that version's section of this file as its notes.
- `docs/validation_checklist.md`: what only the glasses can confirm after an update, and
  what to report.
- Type annotations that hold with the numpy versions Python 3.10 gets (`NDArray[Any]`);
  the repository URLs point at Elios-Lab.

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
