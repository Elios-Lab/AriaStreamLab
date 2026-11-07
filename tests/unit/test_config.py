from __future__ import annotations

import json
from pathlib import Path

import pytest

from ariastreamlab.config import (
    ExperimentConfig,
    deep_merge,
    dump_resolved,
    export_json_schema,
    load_config,
    load_dict,
    load_preset,
    preset_names,
)
from ariastreamlab.core.bus import DropPolicy
from ariastreamlab.core.errors import ConfigError


def _base(**over: object) -> dict[str, object]:
    d: dict[str, object] = {"experiment": {"name": "t"}, "streams": {"rgb": {}, "imu": {}}}
    d.update(over)
    return d


def test_minimal_config_gets_defaults() -> None:
    cfg = load_dict({"experiment": {"name": "first"}})
    assert cfg.experiment.participant == "P000"
    assert cfg.device.connection.streaming_interface == "wifi_sap"
    assert cfg.device.connection.effective_batch_period_ms == 800
    assert cfg.storage.root == Path("./sessions")
    assert cfg.enabled_streams() == {}


def test_enabled_streams_apply_registry_defaults() -> None:
    cfg = load_dict(_base())
    streams = cfg.enabled_streams()
    assert streams["rgb"] == {"fps": 10, "resolution": "2016x1512"}
    assert streams["imu_left"] == {"rate_hz": 800}
    assert "imu_right" in streams
    cfg2 = load_dict(
        {"experiment": {"name": "t"}, "streams": {"rgb": {"enabled": False}, "imu_left": {}}}
    )
    assert list(cfg2.enabled_streams()) == ["imu_left"]


@pytest.mark.parametrize(
    ("config", "fragment"),
    [
        (_base(streams={"rgb": {"fps": 25}}), "streams.rgb.fps: 25 is not supported"),
        (_base(streams={"rgb": {"framerate": 10}}), "unknown parameter"),
        (_base(streams={"imu": {}, "imu_left": {}}), "configured twice"),
        (_base(streams={"rgbb": {}}), "did you mean 'rgb'"),
        (_base(streams={"device_calibration": {}}), "always captured"),
        (_base(procesors=[]), "Extra inputs are not permitted"),
        (
            _base(processors=[{"id": "a", "type": "x", "inputs": ["vio"], "outputs": ["o"]}]),
            "not enabled",
        ),
        (
            _base(processors=[{"id": "a", "type": "x", "inputs": ["ghost"], "outputs": ["o"]}]),
            "unknown stream 'ghost'",
        ),
        (
            _base(processors=[{"id": "a", "type": "x", "inputs": ["rgb"], "outputs": ["rgb"]}]),
            "clashes with a raw stream",
        ),
        (
            _base(
                processors=[
                    {"id": "a", "type": "x", "inputs": ["rgb"], "outputs": ["o"]},
                    {"id": "a", "type": "x", "inputs": ["rgb"], "outputs": ["p"]},
                ]
            ),
            "duplicate id",
        ),
        (
            _base(
                processors=[
                    {"id": "a", "type": "x", "inputs": ["rgb"], "outputs": ["o"]},
                    {"id": "b", "type": "x", "inputs": ["rgb"], "outputs": ["o"]},
                ]
            ),
            "produced by both",
        ),
        (
            _base(
                processors=[
                    {"id": "a", "type": "x", "inputs": ["b_o"], "outputs": ["a_o"]},
                    {"id": "b", "type": "x", "inputs": ["a_o"], "outputs": ["b_o"]},
                ]
            ),
            "cycle detected",
        ),
        (
            _base(
                processors=[
                    {
                        "id": "a",
                        "type": "x",
                        "inputs": ["rgb"],
                        "outputs": ["o"],
                        "execution": {"drop_policy": "block"},
                    }
                ]
            ),
            "stall",
        ),
        (_base(dashboard={"show": ["nope"]}), "dashboard.show"),
        (_base(schema_version=2), "schema_version 2 is not supported"),
        (_base(device={"profile": {"mode": "named"}}), "profile.name is required"),
    ],
)
def test_validation_errors_are_readable(config: dict[str, object], fragment: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_dict(config)
    assert fragment in str(exc.value)
    assert "inspect-config" in str(exc.value)


def test_processor_chain_and_derived_streams() -> None:
    cfg = load_dict(
        _base(
            processors=[
                {
                    "id": "small",
                    "type": "image.resize",
                    "inputs": ["rgb"],
                    "outputs": ["rgb_small"],
                },
                {
                    "id": "gray",
                    "type": "image.grayscale",
                    "inputs": ["rgb_small"],
                    "outputs": ["rgb_gray"],
                    "enabled": False,
                },
                {
                    "id": "edge",
                    "type": "x",
                    "inputs": ["rgb_small", "imu_left"],
                    "outputs": ["edges"],
                    "execution": {"drop_policy": "latest", "join": "nearest"},
                },
            ],
            dashboard={"show": ["rgb", "edges"]},
        )
    )
    assert list(cfg.derived_streams()) == ["rgb_small", "edges"]
    assert cfg.processors[2].execution.drop_policy is DropPolicy.LATEST
    assert cfg.all_stream_ids() == ("rgb", "imu_left", "imu_right", "rgb_small", "edges")


def test_deep_merge_rules() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    assert deep_merge(base, {"a": {"y": [3]}, "c": 2}) == {"a": {"x": 1, "y": [3]}, "b": 1, "c": 2}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 1}  # not mutated


def test_presets_load_and_chain() -> None:
    names = preset_names()
    assert {"base", "rgb_imu", "gaze_experiment", "full_capture", "mock_experiment"} <= set(names)
    for n in names:
        cfg = load_preset(n)
        assert isinstance(cfg, ExperimentConfig)
    gaze = load_preset("gaze_experiment")
    assert gaze.device.connection.streaming_interface == "wifi_sap"  # inherited from base
    assert "eye_gaze" in gaze.enabled_streams()
    assert gaze.processors[0].id == "gaze_on_rgb"


def test_extends_file_relative_and_preset(tmp_path: Path) -> None:
    parent = tmp_path / "parent.json"
    parent.write_text(json.dumps({"extends": "preset:rgb_imu", "storage": {"root": "/data"}}))
    child = tmp_path / "sub" / "child.json"
    child.parent.mkdir()
    child.write_text(
        json.dumps(
            {
                "extends": "../parent.json",
                "experiment": {"name": "child", "participant": "P7"},
                "streams": {"rgb": {"fps": 30}},
            }
        )
    )
    cfg = load_config(child)
    assert cfg.experiment.name == "child"
    assert cfg.experiment.participant == "P7"
    assert cfg.storage.root == Path("/data")
    assert cfg.enabled_streams()["rgb"]["fps"] == 30  # child overrides
    assert cfg.enabled_streams()["imu_left"] == {"rate_hz": 800}  # grandparent kept
    assert cfg.extends is None
    resolved = json.loads(dump_resolved(cfg))
    assert "extends" not in resolved
    assert resolved["experiment"]["name"] == "child"


def test_extends_errors(tmp_path: Path) -> None:
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"extends": "preset:nope", "experiment": {"name": "x"}}))
    with pytest.raises(ConfigError, match="unknown preset"):
        load_config(f)
    f.write_text(json.dumps({"extends": "missing.json", "experiment": {"name": "x"}}))
    with pytest.raises(ConfigError, match="not found"):
        load_config(f)
    f.write_text("{ bad json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(f)
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "ghost.json")
    loop = tmp_path / "loop.json"
    loop.write_text(json.dumps({"extends": "loop.json", "experiment": {"name": "x"}}))
    with pytest.raises(ConfigError, match="too deep"):
        load_config(loop)


def test_json_schema_mentions_streams_and_parameters() -> None:
    schema = export_json_schema()
    streams = schema["properties"]["streams"]["properties"]
    assert streams["rgb"]["properties"]["fps"]["enum"] == [5, 10, 30]
    assert streams["imu"]["properties"]["rate_hz"]["default"] == 800
    assert streams["rgb"]["additionalProperties"] is False
    assert "processors" in schema["properties"]


# --------------------------------------------------------------------------- review fixes


@pytest.mark.parametrize(
    ("stream", "params", "fragment"),
    [
        ("wifi_beacons", {"period_ms": "5000"}, "expected a number, got str"),
        ("gps", {"rate_hz": True}, "expected a number, got bool"),
        ("wifi_beacons", {"period_ms": float("nan")}, "not a finite number"),
        ("wifi_beacons", {"period_ms": float("inf")}, "not a finite number"),
        ("rgb", {"resolution": 2016}, "expected str, got int"),
        ("rgb", {"fps": "10"}, "expected a number, got str"),
    ],
)
def test_wrong_typed_stream_parameters_are_config_errors(
    stream: str, params: dict[str, object], fragment: str
) -> None:
    # before: a TypeError traceback ("5000" < 1000) or silently accepted (true == 1, NaN)
    with pytest.raises(ConfigError, match=fragment):
        load_dict({"experiment": {"name": "t"}, "streams": {stream: params}})


def test_numeric_parameters_still_accept_ints_and_floats() -> None:
    cfg = load_dict(
        {
            "experiment": {"name": "t"},
            "streams": {"wifi_beacons": {"period_ms": 2500.0}, "als": {"rate_hz": 10}},
        }
    )
    assert cfg.enabled_streams()["wifi_beacons"]["period_ms"] == 2500.0


def test_disabled_preset_group_lets_a_member_be_selected(tmp_path: Path) -> None:
    parent = tmp_path / "base.json"
    parent.write_text(json.dumps({"experiment": {"name": "b"}, "streams": {"slam": {"fps": 30}}}))
    child = tmp_path / "child.json"
    child.write_text(
        json.dumps(
            {
                "extends": "base.json",
                "experiment": {"name": "c"},
                "streams": {"slam": {"enabled": False}, "slam_front_left": {}},
            }
        )
    )
    cfg = load_config(child)  # before: "'slam_front_left' is configured twice"
    assert list(cfg.enabled_streams()) == ["slam_front_left"]
    assert cfg.enabled_streams()["slam_front_left"]["fps"] == 10


def test_disabled_member_is_left_out_of_an_enabled_group() -> None:
    cfg = load_dict(
        {
            "experiment": {"name": "t"},
            "streams": {"slam": {"fps": 30}, "slam_side_right": {"enabled": False}},
        }
    )
    assert sorted(cfg.enabled_streams()) == [
        "slam_front_left",
        "slam_front_right",
        "slam_side_left",
    ]
    assert {p["fps"] for p in cfg.enabled_streams().values()} == {30}


def test_two_enabled_entries_for_one_stream_are_still_an_error() -> None:
    with pytest.raises(ConfigError, match="configured twice"):
        load_dict({"experiment": {"name": "t"}, "streams": {"slam": {}, "slam_side_left": {}}})


def test_rgb_full_resolution_is_limited_to_10_fps() -> None:
    with pytest.raises(ConfigError, match="2560x1920 is only available up to 10 fps"):
        load_dict(
            {
                "experiment": {"name": "t"},
                "streams": {"rgb": {"resolution": "2560x1920", "fps": 30}},
            }
        )
    for fps in (5, 10):
        load_dict(
            {
                "experiment": {"name": "t"},
                "streams": {"rgb": {"resolution": "2560x1920", "fps": fps}},
            }
        )
    load_dict({"experiment": {"name": "t"}, "streams": {"rgb": {"fps": 30}}})


def test_custom_profile_needs_its_json() -> None:
    # before: validated, then failed only when connecting to the glasses
    with pytest.raises(ConfigError, match="custom_json is required"):
        load_dict({"experiment": {"name": "t"}, "device": {"profile": {"mode": "custom"}}})


def test_relative_paths_follow_the_file_that_declares_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared"
    (shared / "procs").mkdir(parents=True)
    (shared / "base.json").write_text(
        json.dumps(
            {
                "experiment": {"name": "b"},
                "processors_path": "procs",
                "device": {"profile": {"mode": "custom", "custom_json": "profile.json"}},
            }
        )
    )
    study = tmp_path / "study"
    study.mkdir()
    (study / "experiment.json").write_text(
        json.dumps(
            {
                "extends": "../shared/base.json",
                "experiment": {"name": "s"},
                "storage": {"root": "./sessions"},
            }
        )
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the CLI started from another directory
    cfg = load_config(Path("../study/experiment.json"))
    assert cfg.processors_path == (shared / "procs").resolve()  # before: study/procs
    assert cfg.device.profile.custom_json == (shared / "profile.json").resolve()  # before: cwd
    assert cfg.storage.root == (study / "sessions").resolve()  # before: elsewhere/sessions


def test_a_child_path_overrides_and_anchors_to_the_child(tmp_path: Path) -> None:
    (tmp_path / "base.json").write_text(
        json.dumps({"experiment": {"name": "b"}, "storage": {"root": "parent_sessions"}})
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "e.json").write_text(
        json.dumps({"extends": "../base.json", "experiment": {"name": "c"}})
    )
    assert load_config(sub / "e.json").storage.root == (tmp_path / "parent_sessions").resolve()
    (sub / "e.json").write_text(
        json.dumps(
            {"extends": "../base.json", "experiment": {"name": "c"}, "storage": {"root": "mine"}}
        )
    )
    assert load_config(sub / "e.json").storage.root == (sub / "mine").resolve()


def test_dict_configs_and_presets_keep_relative_paths() -> None:
    assert load_dict({"experiment": {"name": "t"}}).storage.root == Path("sessions")
    assert not load_preset("base").storage.root.is_absolute()
