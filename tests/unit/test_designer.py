"""The experiment designer model and the browser-run plumbing (no NiceGUI needed)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ariastreamlab.config.loader import load_config
from ariastreamlab.config.models import DashboardConfig, ExecutionConfig, ExperimentConfig
from ariastreamlab.dashboard.app import Dashboard
from ariastreamlab.dashboard.designer import Designer, fields_of

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def test_fields_of_flattens_models() -> None:
    specs = {f.name: f for f in fields_of(ExecutionConfig)}
    assert specs["queue_size"].kind == "int"
    assert (specs["queue_size"].minimum, specs["queue_size"].maximum) == (1, 10_000)
    assert specs["drop_policy"].kind == "choice" and "latest" in specs["drop_policy"].choices
    assert specs["drop_policy"].default == "drop_oldest"
    assert specs["join"].choices == ("latest", "nearest", "previous")
    assert specs["tolerance_ms"].kind == "float"
    top = {f.name: f for f in fields_of(ExperimentConfig)}
    assert top["device"].kind == "model" and top["device"].model is not None
    assert top["processors_path"].optional and top["processors_path"].kind == "str"
    assert "schema_version" not in top and "streams" not in top  # dicts get dedicated widgets


def test_designer_from_preset_validates_and_compacts(tmp_path: Path) -> None:
    d = Designer.from_preset("gaze_experiment", base_dir=tmp_path)
    rep = d.validate()
    assert rep.ok and rep.plan is not None and rep.plan.profile_name == "profile9"
    assert any("5 Hz" in w for w in rep.warnings)  # the live plan's measured-rate warning
    compact = d.to_dict()
    assert compact["streams"] == {"rgb": {}, "et": {}, "eye_gaze": {}, "imu": {}}
    assert compact["processors"][0]["execution"] == {
        "queue_size": 2,
        "drop_policy": "latest",
        "join": "nearest",
        "tolerance_ms": 40.0,
    }
    assert "device" not in compact  # all defaults
    # the compact file loads back to the same configuration as the full document
    p = d.save("exp.json")
    assert p == tmp_path / "exp.json"
    cfg = load_config(p)
    full = ExperimentConfig.model_validate(json.loads(d.to_json(compact=False)))
    assert cfg.enabled_streams() == full.enabled_streams()  # defaults were dropped, not lost
    a, b = cfg.model_dump(mode="json"), full.model_dump(mode="json")
    a.pop("streams"), b.pop("streams")
    assert a == b
    assert Designer.from_file(p).to_json() == d.to_json()


def test_designer_streams_and_processors() -> None:
    d = Designer.from_preset("base")
    assert d.validate().ok
    assert d.validate().warnings[0] == "no stream is enabled"
    d.set_stream_enabled("imu", True)
    d.set_stream_enabled("rgb", True)
    d.set_stream_param("rgb", "fps", 30)
    d.set_stream_param("rgb", "resolution", "2016x1512")  # the default: not written
    assert d.doc["streams"]["rgb"] == {"fps": 30}
    assert d.stream_param("rgb", "fps") == 30 and d.stream_param("rgb", "resolution") == "2016x1512"
    assert d.input_options(0) == ["imu_left", "imu_right", "rgb"]

    i = d.add_processor("imu.magnitude")
    p = d.doc["processors"][i]
    assert p["id"] == "magnitude" and p["inputs"] == ["imu_left"] and p["outputs"] == ["magnitude"]
    assert p["parameters"] == {"of": "accel", "remove_gravity": False}
    assert p["execution"]["queue_size"] is None  # auto: half a second of the primary input
    j = d.add_processor("imu.magnitude")
    assert d.doc["processors"][j]["id"] == "magnitude_2"
    assert d.doc["processors"][j]["outputs"] == ["magnitude_2"]  # unique output too
    assert d.validate().ok, d.validate().errors
    d.doc["processors"][j]["outputs"] = ["magnitude"]
    assert any("produced by both" in e for e in d.validate().errors)
    d.doc["processors"][j]["outputs"] = ["magnitude_2"]
    assert d.input_options(j) == ["imu_left", "imu_right", "rgb", "magnitude"]
    assert d.all_stream_ids()[-2:] == ["magnitude", "magnitude_2"]

    d.doc["processors"][j]["parameters"]["of"] = "nope"
    errs = d.validate().errors
    assert len(errs) == 1 and "magnitude_2" in errs[0] and "'accel' or 'gyro'" in errs[0]
    d.doc["processors"][j]["parameters"]["of"] = "gyro"
    d.set_stream_enabled("imu", False)
    errs = d.validate().errors
    assert errs and "not enabled" in errs[0]
    d.set_stream_enabled("imu", True)

    assert d.move_processor(j, -1) == 0
    assert d.doc["processors"][0]["id"] == "magnitude_2"
    d.remove_processor(0)
    assert [p["id"] for p in d.doc["processors"]] == ["magnitude"]
    out = d.to_dict()
    assert out["processors"] == [
        {
            "id": "magnitude",
            "type": "imu.magnitude",
            "inputs": ["imu_left"],
            "outputs": ["magnitude"],
        }
    ]
    assert out["streams"] == {"imu": {}, "rgb": {"fps": 30}}


def test_designer_preview_stays_compact_when_invalid() -> None:
    d = Designer.from_preset("base")
    i = d.add_processor("image.resize")
    d.doc["processors"][i]["inputs"] = []  # invalid: a processor needs an input
    d.doc["storage"]["image_format"] = "png"
    assert not d.validate().ok
    out = d.to_dict()
    assert out["processors"][0]["inputs"] == [] and "execution" not in out["processors"][0]
    assert "parameters" not in out["processors"][0]  # all defaults
    assert out["storage"] == {"image_format": "png"}
    assert "device" not in out and out["experiment"] == {"name": "base"}


def test_designer_finds_file_processors() -> None:
    d = Designer.from_file(EXAMPLES / "custom_processor" / "experiment.json")
    assert d.processors_path() == EXAMPLES / "custom_processor" / "processors"
    types = {e.type for e in d.catalogue()}
    assert "blink_counter.py:BlinkCounter" in types and "imu.motion_detector" in types
    blink = d.entry_for("blink_counter.py:BlinkCounter")
    assert blink is not None and {f.name for f in blink.parameters} == {"min_ms", "max_ms"}
    rep = d.validate()
    assert rep.ok, rep.errors
    assert d.to_dict()["processors"][0]["parameters"] == {"max_ms": 400}  # min_ms 50 = default
    d.doc["processors"][0]["parameters"]["max_ms"] = 500.0
    assert "parameters" not in d.to_dict()["processors"][0]


class _FakeExperiment:
    def __init__(self, config: ExperimentConfig, *, mock: bool, duration_s: float | None) -> None:
        self.config = config
        self.mock = mock
        self.duration_s = duration_s
        self.prompt: Any = None
        self.session_dir = None
        self._stop = threading.Event()
        self.entered = False

    def __enter__(self) -> _FakeExperiment:
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def run(self, duration_s: float | None = None, *, install_signal_handler: bool = True) -> None:
        if self.prompt is not None and not self.mock:
            self.prompt("join the hotspot now")
        self._stop.wait(self.duration_s or 5.0)

    def stop(self) -> None:
        self._stop.set()


def test_dashboard_launch_runs_the_designed_experiment(tmp_path: Path) -> None:
    dash = Dashboard(DashboardConfig(port=18999))
    made: list[_FakeExperiment] = []

    def launcher(config: ExperimentConfig, *, mock: bool, duration_s: float | None, prompt: Any):
        exp = _FakeExperiment(config, mock=mock, duration_s=duration_s)
        exp.prompt = prompt
        made.append(exp)
        return exp

    d = Designer.from_preset("rgb_imu", base_dir=tmp_path)
    assert dash.run_state() is None
    with pytest.raises(RuntimeError, match="no launcher"):
        dash.launch(d, mock=True, duration_s=1.0)
    dash.launcher = launcher
    dash.launch(d, mock=False, duration_s=None)
    exp = made[0]
    # the config was rebuilt for *this* server and the designer's directory
    assert exp.config.dashboard.port == 18999 and exp.config.dashboard.open_browser is False
    assert exp.config.storage.root == (tmp_path / "sessions").resolve()
    assert exp.mock is False
    deadline = time.monotonic() + 3
    while dash.pending_prompt() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    pending = dash.pending_prompt()
    assert pending is not None and pending[1] == "join the hotspot now"
    state = dash.run_state()
    assert state is not None and state["running"] and state["origin"] == "live"
    with pytest.raises(RuntimeError, match="still running"):
        dash.launch(d, mock=True, duration_s=1.0)
    dash.answer_prompt()
    deadline = time.monotonic() + 3
    while dash.pending_prompt() is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert dash.pending_prompt() is None
    dash.stop_run()
    deadline = time.monotonic() + 3
    while dash.run_state()["running"] and time.monotonic() < deadline:  # type: ignore[index]
        time.sleep(0.01)
    state = dash.run_state()
    assert state is not None and state["state"] == "finished" and state["error"] is None
    # a second one can start now
    dash.launch(d, mock=True, duration_s=0.1)
    time.sleep(0.5)
    assert made[1].mock is True and dash.run_state()["state"] == "finished"  # type: ignore[index]
