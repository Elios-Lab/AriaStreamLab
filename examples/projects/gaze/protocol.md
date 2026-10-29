# Session-day protocol — gaze on fixation targets

One participant, about ten minutes including setup. Print this page.

## Before the participant arrives

- [ ] Glasses charged above 60 %; cable **unplugged** during recording (charging heats the
      device; the stream stops at 42 °C).
- [ ] Target sheet at about 1 m from the chair, centre target at eye height; the five targets
      visible in the RGB frame — check on the monitor's RGB image once.
- [ ] `ariastreamlab doctor --device` with the glasses on the cable: all green.
- [ ] Hotspot up and the laptop on it (`aria_gen2 device hotspot status`, or let the script
      start it and join when asked).
- [ ] Free disk: a session is about 150 MB.

## With the participant

1. Fit the glasses; the nose pads must sit so that both eye cameras see the pupils (the
   monitor shows `et_left` / `et_right`).
2. `python run_session.py P<id>` — join the hotspot if asked, wait for the monitor.
3. On the monitor, check before the first trial: **Streams** card, every stream at its
   expected rate; **images**, both eyes visible, a gaze circle on the RGB frame moving with
   the eyes; **Device** card, temperature below 38 °C, not charging.
4. Explain: "look at the target I name, keep looking until I say *done*, blink normally".
5. For each of the ten trials: name the target, wait until the participant is on it, press
   ENTER, wait for the `recorded` line (3 s), say *done*.
6. If anything goes wrong (the participant moved, the glasses slipped) finish the trial
   anyway and add a note on the monitor's Markers card (*note*, e.g. "trial 4 slipped") — it
   is easier to exclude later than to restart.
7. After the last trial the script ends the session and prints the quality summary.

## Before the participant leaves

- [ ] The summary has **no warnings**, or only ones you understand. `eye_gaze` received
      about 30 × duration samples; `gaze_rgb` the same.
- [ ] `ariastreamlab inspect sessions/<folder> --events` lists 10 `fixation` intervals
      of about 3 s each.
- [ ] If not: run the session again — five minutes now is cheaper than a lost participant.

## Afterwards

- Participant id, date, target distance and anything unusual in your lab notebook or in the
  `description` field (the manifest keeps the file's description; notes added on the monitor
  are events in the session).
- `python analyze.py sessions/<folder>` for a first look.
