# Session-day protocol — visual search on a desk

One participant, about fifteen minutes. Two boxes of objects: the 6 "sparse" ones and the
6 extra ones that make the cluttered layout; the eight targets of `run_session.py` among
them. Print this page and the layout photos you took during the pilot.

## Before the participant arrives

- [ ] Glasses charged above 60 %, cable unplugged while recording.
- [ ] Desk at normal working distance (60–80 cm); the whole desk inside the RGB frame when
      the participant sits upright — check on the monitor during the pilot and keep the chair
      position.
- [ ] `ariastreamlab doctor --device` all green; hotspot up and the laptop on it.
- [ ] Layouts prepared from the photos; the targets of trials 1–8 noted on this page.

## With the participant

1. Fit the glasses (both pupils visible in `et_left` / `et_right` on the monitor).
2. `python run_session.py P<id>`; wait for the monitor.
3. On the monitor: all streams at their rates; a gaze circle moving on the RGB image;
   `head_motion` flips to 1 when the participant turns the head and back to 0 at rest (if it
   is 1 at rest, raise `threshold` in the Pipeline → *parameters* dialog — the change is
   recorded as an event).
4. Explain: "I will name an object; look for it and say *found* as soon as you see it; keep
   your hands on your lap". Ask the participant to close the eyes while you arrange the
   desk between trials.
5. For each trial: arrange the layout, name the target, say *go* and press ENTER at the same
   time; press ENTER when the participant says *found*; `n` + ENTER after 20 s if not.
6. Anything unusual: a *note* on the monitor's Markers card, with the trial number.
7. The script ends the session after trial 8 and prints the quality summary.

## Before the participant leaves

- [ ] Summary without warnings; `eye_gaze` and `gaze_rgb` at about 30 Hz, `rgb` at 10.
- [ ] `ariastreamlab inspect sessions/<folder> --events` shows 8 `search` intervals, each
      with `found` in its payload.
- [ ] A quick `python analyze.py sessions/<folder>`: search times plausible (1–20 s), gaze
      valid fraction above 0.8. If a trial went wrong, note it now — the trial number is in
      the CSV.

## Afterwards

- Photograph the final layouts if they changed from the pilot.
- Lab notebook: participant id, date, layouts used, notes.
