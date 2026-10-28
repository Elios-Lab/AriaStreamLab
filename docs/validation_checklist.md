# Hardware validation checklist — 0.2.0

What the 0.2.0 application has *not* yet been tried with real glasses, and what to report
back after each step so that the result can be turned into a fix or a documented finding.
Everything in the framework up to 0.1.1 was verified on hardware (fifteen runs,
`docs/ARCHITECTURE.md` §0); 0.2.0 added the desktop window, the Setup page and the
browser-launched live run, which were verified only against a fake SDK and a headless browser.

Each step says what to do, what should happen, and what to paste back. Where something
looks wrong, paste the terminal output *and* the session's `session.log` and
`quality/summary.txt` — they are small.

Time: about 45 minutes, of which 20 are the long session in step 7. Steps 1–3 need no glasses.

## 0. Before starting

```bash
cd ~/Source/AriaStreamLab
git pull                      # 0.2.0 + CI
# in the environment you used for run 15 (the SDK is already there):
pip install -e .              # picks up nicegui and pywebview
ariastreamlab --version
ariastreamlab doctor
```

Paste: the `doctor` output (the whole table). Expected: everything green except the device
checks, which need the glasses.

## 1. The window opens and closes cleanly

```bash
ariastreamlab
```

Expected: a native window (not a browser tab) titled AriaStreamLab with the four cards
Setup / Design / Monitor / Sessions; closing it ends the process and the terminal prompt comes
back. Then:

```bash
ariastreamlab app --browser
```

Expected: the same pages in your browser at `http://127.0.0.1:8765/`; Ctrl-C ends it.

Paste: whether the window appeared, how long it took, anything printed in the terminal
(especially a line saying it fell back to the browser, or a pywebview / Cocoa error).

- [ ] native window opens
- [ ] closing the window ends the process
- [ ] `--browser` serves the same pages

## 2. Setup page without the glasses

Open **Setup**. The checks run by themselves.

Expected: the Environment card shows the interpreter you expect (the SDK environment, Python
3.10–3.12, no amber warning), the packages table shows every row installed (rerun may be
"not installed" — that is fine), the checks are green except the device ones.

Press **list devices** with no glasses connected. Expected: the command's output appears in
the Output card, then "exited with code …" or "done" with an empty list; no hang.

Paste: a screenshot of the page (Cmd-Shift-4) and the text of the Output card.

- [ ] environment and packages correct
- [ ] checks render
- [ ] a CLI command streams its output and ends

## 3. Setup page with the glasses on the cable

Connect the glasses over USB. Press **run checks with the glasses**, then **check pairing**,
then **hotspot status**.

Expected: the device checks turn green; `auth check` says the laptop is paired; hotspot
status prints the hotspot state (probably down).

If the laptop is *not* paired: press **pair over USB** and accept in the Companion App;
paste the Output card either way.

Do **not** press *install streaming certificates* unless `aria_gen2 auth check` says there is
no certificate — it generates a new one, and the live run installs one by itself when none
exists.

Paste: the Output card after each of the three commands.

- [ ] device checks green
- [ ] pairing confirmed
- [ ] hotspot status printed

## 4. Live run started from the Design page (short, tethered)

Keep the cable connected. Open **Design**, type `examples/eye_gaze.json` (or the file you used
for run 15) in the *load file* box of the header and press *load*, set the duration to
**120** s at the bottom, leave *mock* **off**, press **Run**.

Expected, in order: the plan is validated (no problems in the right column); the page asks
you to join the hotspot in a dialog (SSID and password shown) — join it from the Wi-Fi menu
and press the dialog's button; the **Monitor** page shows the run: images with the gaze
circle, the Streams card with rates close to nominal, the Device card with temperature and
"charging"; after 120 s the session ends by itself; the home page says "last run: finished".

If the dialog never appears or the run fails, press **stop** on the Monitor page and paste
the terminal output.

Paste:

```bash
ariastreamlab inspect sessions/<the new folder>
cat sessions/<the new folder>/quality/summary.txt
```

- [ ] hotspot dialog shown and released by the button
- [ ] monitor shows the run
- [ ] session finished at the duration, summary written

## 5. Markers and a live parameter change (during step 4 or a second short run)

While a run is on the Monitor page: type a name in the Markers card and press *mark*, then
*begin*, wait ten seconds, *end*; open *parameters* in the Pipeline card, change anything
(for `eye_gaze.json` there is nothing to change — use `examples/rgb_processing.json` in a mock
run if you prefer) and *apply*.

Paste: `ariastreamlab inspect sessions/<folder> --events` — expected: a `marker`, one interval
with the right duration, a `config_change` if you applied one, and `clock_anchor` events every
60 s.

- [ ] mark / begin / end recorded with device time
- [ ] parameter change recorded

## 6. Stall detection (deliberate)

Start a 10-minute run from the Design page (cable connected is fine). After a minute, from a
second terminal in the same environment:

```bash
aria_gen2 streaming stop
```

This is what thermal mitigation does from the framework's point of view: the glasses stop
sending. Expected: on the Monitor page the effective rates fall to 0 within about 5 s, the
Link card shows "stalled … s" counting up, and **30 s after the last sample** the session ends
by itself (`on_stall: "stop"`), with a `stream_stalled` warning in the summary and
`streaming_s` about one minute while `duration_s` is about 90 s.

Paste: the terminal output from `streaming stop` onwards, and `quality/summary.txt`.

- [ ] rates fall to 0
- [ ] session ends 30 s after the stall
- [ ] summary says why

## 7. The 20-minute wireless session, *not* charging

This is the measurement that matters for real experiments. Run 15 reached 42 °C after 9½
minutes while charging over USB; the question is whether the glasses stay under 42 °C on
battery.

Charge the glasses first (battery ≥ 70 %). Unplug the cable. Make sure the hotspot is up and
the laptop is on it (`hotspot status` on the Setup page, or just let the run start it). Then:

```bash
ariastreamlab run examples/eye_gaze.json --duration 1200
```

(or from the Design page with duration 1200). Wear the glasses or leave them on the desk —
say which; the skin temperature sensor reacts to both. Watch the Device card now and then.

Expected: the session runs to the end with the temperature rising and levelling off; if it
reaches 42 °C the stream stops and step 6's mechanism ends the session — that is also a result.

Paste:

```bash
cat sessions/<folder>/quality/summary.txt
ariastreamlab inspect sessions/<folder> --events
python -c "import json,sys; [print(h.get('t_s'), h.get('skin_temperature_c'), h.get('battery_percent'), h.get('charging')) for h in map(json.loads, open(sys.argv[1]))]" sessions/<folder>/quality/device_health.jsonl
```

The last command prints the temperature and battery every 5 s; that curve is what goes into
the architecture document.

- [ ] session completed (or ended by the stall mechanism — say which)
- [ ] temperature curve pasted
- [ ] battery start/end pasted

## 8. Device versus host counts (from steps 4 and 7)

`quality/summary.txt` warns when the glasses' own counter is more than 1 % above what the
host received ("the device counted N samples, the host received M"); run 15 showed the host
1.1–1.8 % short with 0 transport drops and no explanation yet. Nothing to do here beyond
pasting the summaries: with a tethered (step 4) and an untethered (step 7) session side by
side the question is whether the gap depends on the link.

## Anything else

- A page that looks wrong: screenshot plus the terminal output.
- The window on a second monitor, resized, or after sleep: whatever you notice.
- Pressing *replay* on the Sessions page for the step-7 session at speed 4 should show the
  same images and rates on the Monitor page without the glasses.
