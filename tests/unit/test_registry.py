from __future__ import annotations

import pytest

from ariastreamlab.core.errors import RegistryError
from ariastreamlab.core.registry import REGISTRY


def test_every_descriptor_is_consistent() -> None:
    ids = REGISTRY.ids()
    assert len(ids) == len(set(ids))
    for d in REGISTRY:
        assert d.id == d.id.lower()
        if d.is_image:
            assert d.sdk_camera_id is not None
            assert d.sdk_callback in {"rgb", "slam", "et"}
        if d.group:
            assert d.id in REGISTRY.expand(d.group)
        for name, spec in d.parameters.items():
            assert spec.name == name
            if spec.choices is not None:
                assert spec.default in spec.choices


def test_known_aria_gen2_streams_present() -> None:
    expected = {
        "rgb",
        "slam_front_left",
        "slam_front_right",
        "slam_side_left",
        "slam_side_right",
        "et_left",
        "et_right",
        "imu_left",
        "imu_right",
        "magnetometer",
        "barometer",
        "audio",
        "gps",
        "ppg",
        "als",
        "temperature",
        "eye_gaze",
        "hand_tracking",
        "vio",
        "vio_high_frequency",
        "device_calibration",
    }
    assert expected <= set(REGISTRY.ids())
    assert REGISTRY.get("rgb").native_label == "camera-rgb"
    assert REGISTRY.get("rgb").native_stream_ids == ("214-1",)
    assert REGISTRY.get("imu_right").native_stream_ids == ("1202-2",)
    assert REGISTRY.get("eye_gaze").native_stream_ids == ("373-1",)
    assert REGISTRY.by_label("slam-side-right").id == "slam_side_right"


def test_streams_without_typed_callbacks_are_flagged() -> None:
    assert not REGISTRY.get("als").live_available
    assert not REGISTRY.get("temperature").live_available
    assert REGISTRY.get("eye_gaze").live_available
    assert "als" not in REGISTRY.live_streams()


def test_groups_expand_and_config_keys() -> None:
    assert REGISTRY.expand("imu") == ("imu_left", "imu_right")
    assert REGISTRY.expand("slam") == (
        "slam_front_left",
        "slam_front_right",
        "slam_side_left",
        "slam_side_right",
    )
    assert REGISTRY.expand("rgb") == ("rgb",)
    assert REGISTRY.is_group("imu")
    assert not REGISTRY.is_group("rgb")
    keys = REGISTRY.config_keys()
    assert "imu" in keys
    assert "imu_left" not in keys
    assert "device_calibration" not in keys
    assert "ppg" in keys


def test_unknown_stream_gives_suggestion() -> None:
    with pytest.raises(RegistryError) as exc:
        REGISTRY.get("eye_gazee")
    assert "did you mean 'eye_gaze'" in str(exc.value)
    with pytest.raises(RegistryError):
        REGISTRY.expand("nope")


def test_parameter_validation_messages() -> None:
    fps = REGISTRY.get("rgb").parameters["fps"]
    assert fps.validate(30, "rgb") == 30
    with pytest.raises(RegistryError) as exc:
        fps.validate(25, "rgb")
    assert "streams.rgb.fps: 25 is not supported" in str(exc.value)
    assert "allowed: 5, 10, 30" in str(exc.value)
    period = REGISTRY.get("wifi_beacons").parameters["period_ms"]
    with pytest.raises(RegistryError, match="below the minimum"):
        period.validate(10, "wifi_beacons")
