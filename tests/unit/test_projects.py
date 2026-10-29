"""The example projects (examples/projects/*) stay runnable: configs validate, the protocol
scripts' annotations are what the analysis scripts expect, and `Experiment.start()` — the API
they are built on — behaves."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

import pytest

from ariastreamlab.api import Experiment
from ariastreamlab.config import load_config, load_dict
from ariastreamlab.storage.reader import SessionReader

PROJECTS = Path(__file__).resolve().parents[2] / "examples" / "projects"
NAMES = ("gaze", "visual_search", "walking")


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem + "_" + path.parent.name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", NAMES)
def test_project_folder_is_complete_and_config_validates(name: str) -> None:
    folder = PROJECTS / name
    for f in ("README.md", "protocol.md", "experiment.json", "run_session.py", "analyze.py"):
        assert (folder / f).exists(), f"{name}/{f} missing"
    cfg = load_config(folder / "experiment.json")
    assert cfg.experiment.name
    assert cfg.enabled_streams()


def _short_session(name: str, annotate: str, payloads: list[dict[str, object]]) -> Path:
    """A 3-second non-realtime mock session of the project's config with a few intervals."""
    data = json.loads((PROJECTS / name / "experiment.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix="asl_proj_"))
    data["storage"] = {"root": str(tmp / "sessions")}
    data["dashboard"] = {"enabled": False}
    data.setdefault("mock", {})["realtime"] = False
    (tmp / "experiment.json").write_text(json.dumps(data), encoding="utf-8")
    exp = Experiment.from_json(tmp / "experiment.json", mock=True, duration_s=3.0, participant="P9")
    with exp:
        exp.start()
        time.sleep(0.3)
        base = None
        while base is None:  # wait for data so that intervals get device time
            s = exp.stream_stats().get("imu_left")
            base = s.last_device_ns if s is not None and s.received else None
            time.sleep(0.05)
        for i, payload in enumerate(payloads):
            t0 = base + int((0.2 + 0.6 * i) * 1e9)
            tid = exp.begin(annotate, payload, at_ns=t0)
            exp.end(tid, at_ns=t0 + int(0.5e9))
        assert exp.running
        assert exp.wait(timeout=60)
        assert not exp.running
    root = exp.session_dir
    assert root is not None
    return root


def test_experiment_start_wait_and_participant_override() -> None:
    root = _short_session("gaze", "fixation", [{"trial": 1, "target": "centre"}])
    r = SessionReader(root)
    assert r.manifest.participant == "P9"
    assert len(r.intervals("fixation")) == 1
    assert r.intervals("fixation")[0].duration_s == pytest.approx(0.5, abs=0.01)


def test_start_twice_is_refused() -> None:
    data = json.loads((PROJECTS / "gaze" / "experiment.json").read_text(encoding="utf-8"))
    data["storage"] = {"root": str(Path(tempfile.mkdtemp(prefix="asl_proj_")) / "sessions")}
    data["dashboard"] = {"enabled": False}
    with Experiment(load_dict(data, base_dir=PROJECTS / "gaze"), mock=True, duration_s=2.0) as exp:
        exp.start()
        with pytest.raises(RuntimeError):
            exp.start()
        exp.stop()
        assert exp.wait(timeout=30)


def test_gaze_analysis_on_a_mock_session() -> None:
    root = _short_session(
        "gaze",
        "fixation",
        [{"trial": 1, "block": 1, "target": "centre"}, {"trial": 2, "block": 1, "target": "left"}],
    )
    mod = _load(PROJECTS / "gaze" / "analyze.py")
    rows, summary = mod.analyse(root)
    assert [r["target"] for r in rows] == ["centre", "left"]
    assert summary["fixations"] == 2
    assert all(0 <= float(r["x_norm"]) <= 1 for r in rows if "x_norm" in r)


def test_visual_search_analysis_on_a_mock_session() -> None:
    root = _short_session(
        "visual_search",
        "search",
        [
            {"trial": 1, "target": "pen", "layout": "sparse", "n_objects": 6, "found": True},
            {"trial": 2, "target": "mug", "layout": "cluttered", "n_objects": 12, "found": False},
        ],
    )
    mod = _load(PROJECTS / "visual_search" / "analyze.py")
    rows = mod.analyse(root)
    assert [r["found"] for r in rows] == [True, False]
    assert rows[0]["search_time_s"] == pytest.approx(0.5, abs=0.01)
    assert "head_moves" in rows[0]


def test_walking_analysis_on_a_mock_session() -> None:
    root = _short_session("walking", "lap", [{"lap": 1, "pace": "normal"}])
    mod = _load(PROJECTS / "walking" / "analyze.py")
    rows, summary = mod.analyse(root)
    assert len(rows) == 1
    assert rows[0]["poses"] > 0
    assert float(rows[0]["distance_m"]) > 0
    assert "altitude_change_m" in rows[0]
    assert summary["participant"] == "P9"
