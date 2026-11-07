# Changelog

## Unreleased

- Live sessions release the glasses' control connection when they end (it stayed open for
  the life of the process, one more per dashboard session). If `start_streaming` fails
  after the glasses may already have started (a timeout), the session now stops streaming
  before giving up; before, the next run was refused with "already streaming".
- The time-sync reference is read only while the glasses are still connected. The check now
  runs right before the read, not before the ~21 s Wi-Fi control hand-over, so a device
  that left during it is no longer read (the run-12 bus error).
- Closing a session started with `start()` never stops the source while the run thread is
  still stopping it. A live stop can outlast the 30 s wait, and two concurrent stops of the
  SDK risked the teardown crash. `close()` waits up to 2 more minutes, then takes its turn.
- SDK replay of a VRS file waits until the callbacks go quiet before releasing its handler,
  as live sessions do (the crash pattern of hardware run 10).
- A recorder thread still writing when its time to drain is up is no longer closed under:
  the writers are closed once its current write ends, the unwritten samples are named in
  the manifest's warnings, and the manifest is written (before: "dictionary changed size
  during iteration", status left at `running`). A failure while writing the events or the
  timeline is a warning instead of an unfinished session, and `logs/session.log` is
  detached whatever happens (before, the next session also logged into it).
- When a processor's `setup()` fails, the processors already started are stopped again
  instead of running on, subscribed to the bus, after the session gave up.
- A busy dashboard port is found before the server starts. The application window now says
  "port 8765 is in use" with a hint instead of flashing and closing, and a session's
  dashboard can start again in the same process once the port is free (before: "Cannot
  add middleware after an application has started").
- SDK replay of a VRS (`--backend sdk`) delivers RGB frames: they reached it still H.265
  encoded and every one failed (`bad_optional_access`; a probe recording gave 0 of 40
  frames, now 40 at 2560×1920). It decodes with Meta's `aria.image_decoder`, as its live
  receiver does; a frame that cannot be decoded is counted (`decode_failed`), never passed on.
- The default sessions folder (`./sessions`, when a file has no `storage.root`) is next to
  the experiment file, like a written one; it was the current directory.
- Designer, *save*: the file's `extends` is kept and only what differs from it is written
  (it was flattened into the preset's values). Saving into another folder rewrites relative
  paths so they point where they did. The file name defaults to the file that was opened
  (it was always `experiment.json`), and saving over any other existing file asks first.
- `ariastreamlab design` with a missing or invalid file says what is wrong and exits with
  code 2, like `run`; it printed a traceback.
- Sessions page: a session's details (its size on disk, which walks every file) load off
  the interface's event loop; a large session froze every page until they were read.
- Reading a session: samples are in time order across chunk files too (a late sample at a
  flush boundary came out of order, and `iter_all` merged on that order). An unreadable
  chunk is listed in `SessionReader.read_errors` instead of silently skipped. Videos are
  read frame after frame instead of seeking before each frame. Chunk files are flushed to
  disk before they are renamed into place.
- `imu.lowpass` (version 2) and `imu.motion` (version 3) are not reset by a sample stamped
  at or before the previous one; one late glitch went straight through or read as motion.
- Pairing checks (`doctor`, `auth check`) no longer read "unauthenticated" as paired. The
  `aria_gen2` command failing to start is a readable device error, and stray bytes in its
  output no longer raise. Joining the glasses' hotspot on macOS reads the output of
  `networksetup`, which reports "Could not find network" with exit code 0.
- Statistics: the publish counters of the event bus no longer lose increments when several
  threads publish (about 60 % were lost under contention), and a stream losing many
  samples no longer re-sorts its whole list of missing sequence numbers on every new gap
  (≈ 5 s per 10 000 gaps, under the statistics lock). An empty subscription is truthy.
- `TimeSeriesBuffer.interpolate(ts, interp_imu)` (or `interp_pose`) works on a buffer of
  samples; it raised `AttributeError`. `ns_to_timedelta` truncates negative values toward
  zero, as documented.
- `scripts/release.py` refuses to release unless it is on `main` and level with
  `origin/main` (it could tag a feature branch and print `git push origin main`), to a
  version that is higher than the current one, whose tag exists neither locally nor on
  `origin`. A failed commit (a hook, no git identity) puts `pyproject.toml` and
  `CHANGELOG.md` back instead of leaving a half-applied bump.
- Install scripts: an existing `.venv` is reused only when it runs Python 3.10–3.12 (or
  runs at all); otherwise the script says how to remove it. `install.ps1` stops when a
  command fails (PowerShell's error preference does not cover programs, so a failed
  `pip install` still ended with "Environment ready"), and `-Sdk` explains that Meta's
  packages have no Windows build instead of failing in pip. The README and the manual no
  longer suggest streaming from Windows, and say what works there.
- CI: mypy also runs on Python 3.10, whose older numpy / pyarrow / pydantic have broken
  annotations before; the actions run on Node 24 (`actions/checkout` v7.0.1,
  `astral-sh/setup-uv` v10.3.2, `actions/upload-artifact` v7.0.2, still pinned to commits).

## 0.2.5 — 2026-11-07

- Relative paths in an experiment file are relative to that file, whatever directory the
  command starts from: `storage.root` (before, `ariastreamlab run` resolved it against the
  current directory while the application used the file's folder, so the Sessions page did
  not list sessions recorded with `run` from elsewhere), `device.profile.custom_json`
  (before, the current directory) and, in a file reached through `extends`, its own
  `processors_path` and paths (before, the child's folder). `ariastreamlab run
  examples/mock_experiment.json` now writes to `examples/sessions/`.
- Replaying a recorded session writes the new session next to it; the frozen
  `storage.root` of an older session recorded with `run` (relative) pointed inside the
  recording, and that of a copied session at the other computer's disk.
- Stream parameters of the wrong type are configuration errors naming the key instead of a
  traceback (`"period_ms": "5000"`), and `true` or `NaN` are no longer accepted as numbers
  (`"rate_hz": true` passed as 1).
- A disabled stream entry switches its streams off instead of clashing with a group:
  `"slam": {"enabled": false}` and `"slam_front_left": {}` selects one camera of a preset's
  SLAM group (before: "configured twice"), and `"slam_side_right": {"enabled": false}` next
  to an enabled `slam` streams the other three.
- `rgb` at 2560x1920 above 10 fps is refused when the file is checked (the limit was only
  written in the description), and `device.profile.mode: "custom"` without `custom_json`
  likewise (before: at connect time, with the glasses on).
- Processor files are cached by path and reloaded when they change: two experiments with
  their own `processors/filter.py` in one application each get their own class, and an
  edited file is used by the next run without restarting. A configured `processors_path`
  no longer falls back to a file of the same name in the current directory. Manifests
  name the class as before.
- `generic.downsample` follows live parameter changes (the manifest recorded them but the
  old rate stayed in force), and `gaze.project_to_rgb` re-prepares its projection when its
  `camera` is changed live.

## 0.2.4 — 2026-11-06

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
