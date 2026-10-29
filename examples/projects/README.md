# Project templates

Three complete data-collection projects, each a folder to copy and adapt:
`experiment.json` (what is recorded), `run_session.py` (the protocol with its annotations),
`protocol.md` (the session-day checklist) and `analyze.py` (sessions → table + figure). All
three run on synthetic data without the glasses:

```bash
cd examples/projects/gaze            # or visual_search, walking
python run_session.py P000 --mock --auto
python analyze.py
```

| Project | Question | Recorded | Unit of analysis |
|---|---|---|---|
| [gaze](gaze/) | how accurate and stable is the on-device gaze for this participant | RGB, eye cameras, eye gaze → `gaze_rgb`, IMUs | a 3-s `fixation` on a named target |
| [visual_search](visual_search/) | how long to find an object among 6 or 12 on a desk, and how | as above + `head_motion` | a `search` trial: target, layout, found |
| [walking](walking/) | distance, speed, cadence and head movement on a known loop | VIO, IMUs, barometer, RGB 5 fps, `head_motion` | a `lap` at a given pace |

[docs/manual.md](../../docs/manual.md) is the manual that goes with them: the generic path
from a research question to analysed sessions, then each project in detail.
