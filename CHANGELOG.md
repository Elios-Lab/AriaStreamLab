# Changelog

## Unreleased

- Lossless processing (max-speed replay, non-realtime mock) is now lossless and
  reproducible for processors with several inputs: secondary inputs use back-pressure
  instead of dropping their oldest samples, time-based joins wait for the data (not for a
  host-time timeout) and are never trimmed, and the join buffers are trimmed by time behind
  the primary. A raw secondary stream with nothing at a frame's time (a gap, or a stream
  that ended) stops holding the frame once the primary is 1 s of device time ahead.
- A processor returning something that is not an `Emit`/tuple or a list of them (a bare
  payload, a float) is counted as an error instead of silently ending its worker — which
  also hung a lossless replay for good; any unexpected error in a worker's loop is counted
  and the worker carries on.
- Quitting the app (closing the window, Ctrl-C in `ariastreamlab app --browser` or
  `ariastreamlab design`) waits for a session started from the browser to finalise; before,
  the process exited first and left its manifest at `status: running`.
- The experiment's `logging` section (`level`, `format`) is applied; it was ignored because
  the import-time defaults were never replaced. Reconfiguring keeps the session's
  `logs/session.log` handler.
- Live sessions: the device monitor survives unexpected errors (thermal mitigation reported
  without a temperature, an unparseable quality score, …) — before, one such error ended the
  thread and with it the stall detection that stops a session the glasses have stopped.
  The quality-stats parser no longer raises on a non-numeric `score`.
- Tests: the example-project tests no longer assume a non-realtime mock session is still
  running 0.3 s after it starts.
- Dashboard, sessions it did not start: Sessions → *delete* refuses the session attached to
  the dashboard whatever started it (`ariastreamlab run`, a notebook, a protocol script) —
  before, only a browser-started run was protected — and a session another process is still
  writing (manifest `running`, a file changed in the last 2 min). *Run* on Design and
  *replay* on Sessions refuse to start while any session is attached, instead of starting a
  second session against the same glasses and taking the dashboard over.
- Dashboard on the network (`host: "0.0.0.0"`): *Design*, *Sessions* and *Setup* — which
  read and write files, delete sessions, install packages and start sessions — answer only
  the computer running the experiment; other machines get the monitor. A warning is logged
  when the server listens beyond loopback, and the tutorial says what is exposed.
- Replay controls act at once: pause, seek, speed and stop interrupt the pacing wait
  instead of waiting for the next sample — across a multi-second hole in a recording they
  were dead for the whole gap (30 s at 0.25×) and `stop()` timed out. Seeking a paused replay
  keeps it paused at the target (it used to resume), and the position shows the target.
- Markers and intervals added during a replay (dashboard buttons, `mark()`/`begin()`/`end()`
  without `at_ns`) are stamped at the replay position; the host-clock estimate they used
  was wrong after a pause, a seek or at any speed other than 1×.
- VRS replay after a seek or with `--start`: sequence numbers continue from each stream's
  record index in the file instead of restarting at 0 (the statistics saw duplicates and
  the recorded `sequence_number` was wrong).
- `Experiment.open()` cleans up when it fails half-way (an unknown processor type, a
  storage or dashboard error): the statistics thread and whatever else had started are
  stopped, and a session directory that already exists is finalised as `crashed` instead of
  staying `running`.
- `Experiment.wait()` raises the error that ended a session started with `start()` (no
  glasses found, a source failure…); before, it returned `True` and a protocol loop went on
  annotating a session that had stopped.
- Ctrl-C ends a session normally (`completed`, with an "interrupted" warning) whether it was
  pressed during `run()` or in the protocol loop of a `start()`ed session; the latter used
  to mark it `crashed`.
- An `Experiment` is one session: `open()`/`run()` after `close()` raise instead of writing
  an empty "completed" session that carried the first one's statistics.
- Release workflow: a tag is published only after the whole CI suite (lint, types, import
  contracts, tests on Ubuntu and macOS, wheel build) passes on that tag; the tag must be
  `vX.Y.Z` and reaches the shell through an environment variable (a crafted tag name given
  to the manual re-publish could run commands); the workflow token is read-only except for
  the publishing job, and every third-party action is pinned to a commit.

## 0.2.3 — 2026-10-29

- A session started from the application window tried to start a second dashboard server
  (NiceGUI: "Unable to register another startup handler") — `Dashboard.start()` now
  recognises the server running on the main thread and reuses it (seen in hardware
  validation step 4).

## 0.2.2 — 2026-10-29

- `scripts/release.py`: every change is a patch version — bumps `pyproject.toml`, dates the
  changelog section, commits and tags in one command.
- Setup page, from the first hardware validation of 0.2: text can be selected and copied in
  the native window; the Output card wraps long lines and has *copy* and *save* buttons; the
  checks keep their detail beside the name; a conda environment is named as such; the
  `device list` check says what is reachable and what is paired instead of echoing the CLI's
  last line; the install buttons use `uv pip` in an environment that has no pip module.
- Design page: every stream has a readable name and a one-line description of what it is
  and what it is for (`Eye-tracking cameras (2)` — "left and right infrared cameras looking
  at the eyes…"), with the configuration key and member streams underneath.
- Native window: *browse…* next to *load file* opens the system file dialog, the *save as*
  box has a folder button (save dialog), the Sessions page's folder box a folder dialog; in
  a browser the text boxes remain.
- Sessions page: *delete* removes a session directory from disk after a confirmation that
  shows its size; the session currently recording cannot be deleted.
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
