# Session-day protocol — walking laps

One participant, about twenty minutes. The route must be the same for everyone: mark the
start line with tape, walk it once yourself with the glasses during the pilot and keep that
session as the reference.

## Before the participant arrives

- [ ] Glasses charged above 70 %; **no cable during the session** (the participant walks
      away from the laptop anyway).
- [ ] Hotspot range: walk the route with the laptop running the monitor during the pilot
      and watch the Link card — if the rates drop to 0 at the far end of the loop, carry the
      laptop or shorten the route. The session survives a brief loss of connection
      (`stall_timeout_s` is 30 s); a longer one ends it.
- [ ] `ariastreamlab doctor --device` all green; hotspot up and the laptop on it.
- [ ] Skin temperature below 35 °C at the start (Device card).

## With the participant

1. Fit the glasses; the participant stands on the start line facing along the route.
2. `python run_session.py P<id>`; the script asks for 5 s of standing still (VIO).
3. On the monitor: `vio` at 10 Hz, the Trajectory card showing a point; `imu_*` at 800 Hz;
   `barometer` at 50 Hz; temperature and battery noted on this page.
4. Explain: "walk the loop at your normal pace; I'll say *go*; keep walking until you are
   back here. Then three more laps slowly, as if you were window-shopping".
5. For each lap: *go* and ENTER as the participant crosses the line; ENTER again when they
   cross it at the end. The script prints the lap time and the skin temperature.
6. **Above 40 °C**: finish the current lap, stop the script (Ctrl-C), let the glasses cool
   for ten minutes, start a second session for the remaining laps (the analysis accepts
   several sessions per participant).
7. Anything unusual (someone in the corridor, a door closed): *note* on the Markers card.

## Before the participant leaves

- [ ] Summary without warnings — in particular no `stream stalled` and no `thermal
      mitigation`; `vio` received about 10 × duration samples.
- [ ] `ariastreamlab inspect sessions/<folder> --events`: 6 `lap` intervals.
- [ ] `python analyze.py sessions/<folder>`: `closure_m` below 1 m for every lap (VIO
      tracked the loop), distances consistent across laps of the same pace, cadence
      100–130 steps/min at normal pace. A lap with closure above 2 m: VIO lost track
      (fast turn in a featureless corridor) — repeat that lap.

## Afterwards

- Note the route length measured with a tape (for the VIO scale check), the lighting
  conditions and the temperature at start and end.
