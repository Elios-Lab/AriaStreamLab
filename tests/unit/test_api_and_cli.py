from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ariastreamlab import Experiment, Sample
from ariastreamlab.cli.main import app
from ariastreamlab.config import load_dict
from ariastreamlab.core.errors import ConfigError

runner = CliRunner(env={"COLUMNS": "200"})
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _fast_config() -> dict[str, object]:
    return {
        "experiment": {"name": "api_test", "participant": "P1"},
        "storage": {"root": str(Path(tempfile.mkdtemp(prefix="asl_test_")) / "sessions")},
        "streams": {"rgb": {"fps": 10}, "imu": {}, "eye_gaze": {}},
        "mock": {"seed": 1, "jitter_ms": 0.0, "realtime": False},
    }


def test_experiment_mock_run_subscribe_and_mark() -> None:
    cfg = load_dict(_fast_config())
    exp = Experiment(cfg, mock=True, duration_s=1.0)
    seen: list[Sample] = []
    lock = threading.Lock()
    with exp:
        assert exp.session is not None
        assert exp.session.origin == "mock"
        exp.subscribe("rgb", lambda s: seen.append(s), policy="drop_oldest", queue_size=1000)
        exp.mark("trial_start", {"trial": 1})
        exp.run()
        exp.mark("trial_end")
        stats = exp.stream_stats()
        assert stats["imu_left"].received == 800
        assert stats["rgb"].received == 10
        summary = exp.summary()
        assert summary["origin"] == "mock"
        assert summary["events"] >= 4
        with lock:
            pass
    assert 1 <= len(seen) <= 10
    assert all(s.stream_id == "rgb" for s in seen)
    names = [e.name for e in exp.events.all()]
    assert names[:2] == ["open", "trial_start"]
    assert "acquisition_start" in names
    assert names[-1] == "trial_end"


def test_experiment_live_not_available_yet() -> None:
    exp = Experiment(load_dict(_fast_config()), mock=False)
    with pytest.raises(ConfigError, match="Phase 4"):
        exp.open()


def test_experiment_rejects_blocking_subscribe() -> None:
    exp = Experiment(load_dict(_fast_config()), mock=True)
    with exp, pytest.raises(ConfigError, match="block"):
        exp.subscribe("rgb", lambda s: None, policy="block")


def test_stop_from_another_thread() -> None:
    cfg = load_dict({**_fast_config(), "mock": {"realtime": True}})
    exp = Experiment(cfg, mock=True)
    with exp:
        threading.Timer(0.3, exp.stop).start()
        exp.run()
        assert exp.stream_stats()["imu_left"].received > 0


def test_cli_streams_presets_schema() -> None:
    assert runner.invoke(app, ["streams"]).exit_code == 0
    assert "eye_gaze" in runner.invoke(app, ["streams", "--live-only"]).stdout
    r = runner.invoke(app, ["presets"])
    assert r.exit_code == 0
    assert "rgb_imu" in r.stdout
    r = runner.invoke(app, ["schema"])
    assert r.exit_code == 0
    assert '"streams"' in r.stdout
    assert "ariastreamlab" in runner.invoke(app, ["--version"]).stdout


def test_cli_inspect_config_ok_and_errors(tmp_path: Path) -> None:
    r = runner.invoke(app, ["inspect-config", str(EXAMPLES / "mock_experiment.json")])
    assert r.exit_code == 0
    assert "motion_detector" in r.stdout
    r = runner.invoke(app, ["inspect-config", str(EXAMPLES / "rgb_imu.json"), "--resolved"])
    assert r.exit_code == 0
    assert json.loads(r.stdout)["experiment"]["name"] == "rgb_imu_capture"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"experiment": {"name": "x"}, "streams": {"rgb": {"fps": 7}}}))
    r = runner.invoke(app, ["inspect-config", str(bad)])
    assert r.exit_code == 2
    assert "fps: 7 is not supported" in r.output


def test_cli_run_mock(tmp_path: Path) -> None:
    cfg = tmp_path / "exp.json"
    cfg.write_text(json.dumps({**_fast_config(), "mock": {"realtime": False}}))
    r = runner.invoke(
        app, ["run", str(cfg), "--mock", "--duration", "0.5", "--report-every", "0", "--json"]
    )
    assert r.exit_code == 0, r.output
    assert "imu_left=400" in r.output
    r = runner.invoke(app, ["run", str(cfg), "--duration", "0.1"])
    assert r.exit_code == 2
    assert "--mock" in r.output


def test_cli_run_replay_inspect_roundtrip(tmp_path: Path) -> None:
    cfg = tmp_path / "exp.json"
    cfg.write_text(
        json.dumps(
            {
                **_fast_config(),
                "storage": {"root": str(tmp_path / "sessions"), "image_format": "png"},
                "mock": {"realtime": False, "seed": 2},
            }
        )
    )
    r = runner.invoke(app, ["run", str(cfg), "--mock", "--duration", "0.5", "--report-every", "0"])
    assert r.exit_code == 0, r.output
    sessions = sorted((tmp_path / "sessions").iterdir())
    assert len(sessions) == 1
    r = runner.invoke(app, ["inspect", str(sessions[0])])
    assert r.exit_code == 0, r.output
    assert "completed" in r.output and "imu_left" in r.output
    r = runner.invoke(app, ["inspect", str(sessions[0]), "--json"])
    d = json.loads(r.stdout)
    assert d["status"] == "completed" and d["raw"]["kind"] == "mock_files"
    r = runner.invoke(
        app, ["replay", str(sessions[0]), "--speed", "0", "--report-every", "0", "--json"]
    )
    assert r.exit_code == 0, r.output
    assert "imu_left=400" in r.output
    assert len(sorted((tmp_path / "sessions").iterdir())) == 2
    r = runner.invoke(
        app,
        [
            "replay",
            str(sessions[0]),
            "--speed",
            "0",
            "--streams",
            "rgb",
            "--no-storage",
            "--report-every",
            "0",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "rgb=5" in r.output and "imu_left" not in r.output.split("\n")[-2]
    r = runner.invoke(app, ["inspect", str(tmp_path)])
    assert r.exit_code == 2 and "manifest.json" in r.output
    r = runner.invoke(app, ["replay", str(tmp_path / "missing")])
    assert r.exit_code == 2
