"""Experiment configuration → :class:`StreamingPlan` (what the device is asked to stream).

The streams section of the experiment selects which callbacks are delivered on the bus; the
*device* streams whatever its profile says. In ``predefined`` mode (the default) we use
``profile9``, the profile exercised in every hardware run so far (docs/ARCHITECTURE.md
§0.8–0.11): all sensors plus on-device eye gaze, hand tracking and VIO, RGB at 5 fps.
``named`` uses the given profile verbatim; ``custom`` sends the JSON file from
``device.profile.custom_json`` through ``HttpStreamingConfig.profile_json`` (the API accepts
it; the field has not been exercised on hardware yet, so the plan carries a warning).
"""

from __future__ import annotations

from pathlib import Path

from ariastreamlab.acquisition.base import StreamingPlan
from ariastreamlab.config.models import ExperimentConfig
from ariastreamlab.core.errors import ConfigError
from ariastreamlab.core.registry import REGISTRY

__all__ = ["DEFAULT_PROFILE", "KNOWN_PROFILE_STREAMS", "build_plan"]

DEFAULT_PROFILE = "profile9"

#: Streams observed live per profile (measured, see §0.8–0.11). Other profiles are unknown.
KNOWN_PROFILE_STREAMS: dict[str, frozenset[str]] = {
    "profile9": frozenset(
        {
            "rgb",
            "slam_front_left",
            "slam_front_right",
            "slam_side_left",
            "slam_side_right",
            "imu_left",
            "imu_right",
            "magnetometer",
            "barometer",
            "audio",
            "ppg",
            "eye_gaze",
            "hand_tracking",
            "vio",
            "vio_high_frequency",
            "device_calibration",
        }
    ),
}

#: Measured live rates for profile9 that differ from the registry's nominal values.
PROFILE9_MEASURED_RATES_HZ: dict[str, float] = {"rgb": 5.0, "eye_gaze": 25.0, "hand_tracking": 25.0}


def build_plan(config: ExperimentConfig, raw_dir: Path | None) -> StreamingPlan:
    """Translate the configuration into a streaming plan (pure, no SDK needed)."""
    dev = config.device
    conn = dev.connection
    enabled = list(config.enabled_streams())
    warnings: list[str] = []
    live = []
    for sid in enabled:
        if REGISTRY.get(sid).live_available:
            live.append(sid)
        else:
            warnings.append(
                f"stream {sid!r} has no live callback in SDK 2.5.1 (it is in the VRS; "
                "replay the session to get it)"
            )
    if "device_calibration" not in live:
        live.append("device_calibration")

    profile_name: str | None
    profile_json: str | None = None
    if dev.profile.mode == "named":
        profile_name = dev.profile.name
    elif dev.profile.mode == "custom":
        if dev.profile.custom_json is None:
            raise ConfigError(
                "device.profile.mode is 'custom' but no device.profile.custom_json is given",
                hint="AriaStreamLab does not generate profiles (the schema is not public); "
                "point custom_json at a profile exported from the device, or use "
                "mode 'predefined' / 'named'",
            )
        p = Path(dev.profile.custom_json)
        if not p.is_file():
            raise ConfigError(f"device.profile.custom_json not found: {p}")
        profile_json = p.read_text(encoding="utf-8")
        profile_name = None
        warnings.append(
            "custom profile JSON is sent via HttpStreamingConfig.profile_json; this path has "
            "not been verified on hardware yet"
        )
    else:
        profile_name = dev.profile.name or DEFAULT_PROFILE

    if profile_name in KNOWN_PROFILE_STREAMS:
        missing = [s for s in live if s not in KNOWN_PROFILE_STREAMS[profile_name]]
        if missing:
            warnings.append(
                f"{profile_name} does not stream {', '.join(missing)} (measured on hardware); "
                "those streams will stay empty"
            )
        if profile_name == DEFAULT_PROFILE:
            for sid, hz in PROFILE9_MEASURED_RATES_HZ.items():
                if sid in live:
                    params = config.enabled_streams().get(sid, {})
                    wanted = params.get("fps") or params.get("rate_hz")
                    if wanted is not None and float(wanted) > hz:
                        warnings.append(
                            f"{sid}: {profile_name} delivers about {hz:g} Hz live "
                            f"(configured {wanted}); the configuration cannot raise it"
                        )
    elif profile_name is not None:
        warnings.append(
            f"profile {profile_name!r} has not been measured; stream availability unknown"
        )

    return StreamingPlan(
        streams=tuple(live),
        interface=conn.streaming_interface,
        batch_period_ms=conn.effective_batch_period_ms,
        profile_name=profile_name,
        profile_json=profile_json,
        record_vrs_path=None
        if raw_dir is None or not config.storage.save_raw_vrs
        else str(raw_dir),
        server_port=conn.server_port,
        warnings=tuple(warnings),
    )
