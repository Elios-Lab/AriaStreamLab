"""Registry of the streams Aria Gen 2 can deliver.

This is the *only* place where the list of streams, their native names, nominal rates and
user-configurable parameters is written down. Configuration validation, the profile
translator, the mock source, the storage layer and the dashboard all read it from here.

Sources of truth (verified against ``projectaria_client_sdk`` 2.5.1 / ``projectaria_tools``
2.3.0 and the official Gen 2 docs, see docs/ARCHITECTURE.md §0.5): the VRS label ↔ stream-id
mapper, the ``StreamDataInterface.register_*_callback`` surface, the pre-defined profile table
and the custom-profile template.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from ariastreamlab.core.errors import RegistryError

Category = Literal[
    "camera",
    "imu",
    "magnetometer",
    "barometer",
    "audio",
    "gps",
    "beacon",
    "bio",
    "env",
    "mp",
    "calib",
]
PayloadKind = Literal[
    "image", "imu", "mag", "baro", "audio", "gps", "gaze", "pose", "hand", "scalar", "dict"
]


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """A user-configurable stream parameter and its allowed values.

    Attributes:
        name: Key used in the experiment JSON (e.g. ``"fps"``).
        choices: Allowed values when the parameter is an enumeration, else ``None``.
        minimum, maximum: Numeric bounds when the parameter is a number range.
        default: Value applied when the user omits the parameter.
        profile_field: Name of the corresponding field in Meta's custom-profile JSON.
        description: One line for error messages and the schema.
    """

    name: str
    default: Any
    choices: tuple[Any, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    profile_field: str | None = None
    description: str = ""

    def validate(self, value: Any, stream_id: str) -> Any:
        """Return ``value`` if allowed, else raise a readable :class:`RegistryError`."""
        if self.choices is not None and value not in self.choices:
            allowed = ", ".join(repr(c) for c in self.choices)
            raise RegistryError(
                f"streams.{stream_id}.{self.name}: {value!r} is not supported by Aria Gen 2 "
                f"(allowed: {allowed})",
                hint=self.description or None,
            )
        if self.minimum is not None and value < self.minimum:
            raise RegistryError(
                f"streams.{stream_id}.{self.name}: {value!r} is below the minimum {self.minimum}"
            )
        if self.maximum is not None and value > self.maximum:
            raise RegistryError(
                f"streams.{stream_id}.{self.name}: {value!r} is above the maximum {self.maximum}"
            )
        return value


@dataclass(frozen=True, slots=True)
class StreamDescriptor:
    """Static description of one Aria Gen 2 stream.

    Attributes:
        id: AriaStreamLab stream id, used everywhere in configs, topics and session folders.
        category: Coarse sensor family.
        native_label: VRS label as used by ``projectaria_tools`` (e.g. ``"camera-rgb"``).
        native_stream_ids: VRS stream ids (``"214-1"``). Pose streams are dynamic (``"371-*"``).
        sdk_callback: Suffix of the ``StreamDataInterface.register_<x>_callback`` that delivers
            this stream live, or ``None`` when it is only recorded in VRS / raw messages.
        sdk_camera_id: For image streams, the ``ImageDataRecord.camera_id`` bitmask value.
        profile_key: Section of Meta's custom-profile JSON controlling this stream.
        payload: Kind of payload carried in ``Sample.data``.
        nominal_rate_hz: Rate in the default profile (``None`` when event-driven).
        parameters: User-configurable parameters.
        units: Human-readable units for the dashboard and the storage ``_meta.json``.
        group: Alias group this stream belongs to (``"slam"`` for the four CV cameras,
            ``"imu"`` for the two IMUs); lets one write ``"imu": {...}`` once.
        description: One line for the docs.
    """

    id: str
    category: Category
    native_label: str
    native_stream_ids: tuple[str, ...]
    payload: PayloadKind
    sdk_callback: str | None = None
    sdk_camera_id: int | None = None
    profile_key: str | None = None
    nominal_rate_hz: float | None = None
    parameters: Mapping[str, ParameterSpec] = field(default_factory=lambda: MappingProxyType({}))
    units: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    group: str | None = None
    description: str = ""

    @property
    def is_image(self) -> bool:
        """True for camera streams."""
        return self.payload == "image"

    @property
    def live_available(self) -> bool:
        """True when the SDK delivers this stream through a typed callback."""
        return self.sdk_callback is not None


def _params(*specs: ParameterSpec) -> Mapping[str, ParameterSpec]:
    return MappingProxyType({s.name: s for s in specs})


_RATE_800 = ParameterSpec("rate_hz", 800, choices=(800,), profile_field="rate_hz")
_ENABLED_ONLY = _params()

_RGB_FPS = ParameterSpec(
    "fps", 10, choices=(5, 10, 30), profile_field="rate_hz", description="RGB frame rate"
)
_RGB_RES = ParameterSpec(
    "resolution",
    "2016x1512",
    choices=("2016x1512", "2560x1920"),
    profile_field="resolution",
    description="RGB output size; 2560x1920 is only available up to 10 fps in Meta's profiles",
)
_CV_FPS = ParameterSpec(
    "fps", 10, choices=(10, 30), profile_field="rate_hz", description="CV camera rate"
)
_ET_FPS = ParameterSpec(
    "fps", 5, choices=(5,), profile_field="rate_hz", description="ET camera rate"
)
_MP_RATE_30 = ParameterSpec("rate_hz", 30, choices=(10, 30), profile_field="rate_hz")
_MP_RATE_10 = ParameterSpec("rate_hz", 10, choices=(10,), profile_field="rate_hz")

_IMAGE_UNITS = MappingProxyType({"pixel": "uint8"})
_IMU_UNITS = MappingProxyType({"accel": "m/s^2", "gyro": "rad/s", "temperature": "degC"})


def _camera(
    id_: str,
    label: str,
    sid: str,
    cam_id: int,
    group: str,
    fps: ParameterSpec,
    res: ParameterSpec | None,
    rate: float,
    cb: str,
    key: str,
    desc: str,
) -> StreamDescriptor:
    params = [fps] + ([res] if res is not None else [])
    return StreamDescriptor(
        id=id_,
        category="camera",
        native_label=label,
        native_stream_ids=(sid,),
        payload="image",
        sdk_callback=cb,
        sdk_camera_id=cam_id,
        profile_key=key,
        nominal_rate_hz=rate,
        parameters=_params(*params),
        units=_IMAGE_UNITS,
        group=group,
        description=desc,
    )


_DESCRIPTORS: tuple[StreamDescriptor, ...] = (
    _camera(
        "rgb",
        "camera-rgb",
        "214-1",
        64,
        "rgb",
        _RGB_FPS,
        _RGB_RES,
        10,
        "rgb",
        "rgb_camera",
        "Forward RGB camera (H.265 on the wire, decoded on the host)",
    ),
    _camera(
        "slam_front_left",
        "slam-front-left",
        "1201-1",
        1,
        "slam",
        _CV_FPS,
        None,
        10,
        "slam",
        "slam_cameras",
        "Front-left CV/SLAM camera, 512x512 mono",
    ),
    _camera(
        "slam_front_right",
        "slam-front-right",
        "1201-2",
        2,
        "slam",
        _CV_FPS,
        None,
        10,
        "slam",
        "slam_cameras",
        "Front-right CV/SLAM camera, 512x512 mono",
    ),
    _camera(
        "slam_side_left",
        "slam-side-left",
        "1201-3",
        4,
        "slam",
        _CV_FPS,
        None,
        10,
        "slam",
        "slam_cameras",
        "Side-left CV/SLAM camera, 512x512 mono",
    ),
    _camera(
        "slam_side_right",
        "slam-side-right",
        "1201-4",
        8,
        "slam",
        _CV_FPS,
        None,
        10,
        "slam",
        "slam_cameras",
        "Side-right CV/SLAM camera, 512x512 mono",
    ),
    _camera(
        "et_left",
        "camera-et-left",
        "211-1",
        16,
        "et",
        _ET_FPS,
        None,
        5,
        "et",
        "et_cameras",
        "Left eye-tracking camera, 200x200 IR",
    ),
    _camera(
        "et_right",
        "camera-et-right",
        "211-2",
        32,
        "et",
        _ET_FPS,
        None,
        5,
        "et",
        "et_cameras",
        "Right eye-tracking camera, 200x200 IR",
    ),
    StreamDescriptor(
        "imu_left",
        "imu",
        "imu-left",
        ("1202-1",),
        "imu",
        sdk_callback="imu",
        profile_key="imus",
        nominal_rate_hz=800,
        parameters=_params(_RATE_800),
        units=_IMU_UNITS,
        group="imu",
        description="Left IMU (accelerometer + gyroscope)",
    ),
    StreamDescriptor(
        "imu_right",
        "imu",
        "imu-right",
        ("1202-2",),
        "imu",
        sdk_callback="imu",
        profile_key="imus",
        nominal_rate_hz=800,
        parameters=_params(_RATE_800),
        units=_IMU_UNITS,
        group="imu",
        description="Right IMU (accelerometer + gyroscope)",
    ),
    StreamDescriptor(
        "magnetometer",
        "magnetometer",
        "mag0",
        ("1203-1",),
        "mag",
        sdk_callback="magnetometer",
        profile_key="magnetometer",
        nominal_rate_hz=100,
        parameters=_params(ParameterSpec("rate_hz", 100, choices=(100,), profile_field="rate_hz")),
        units=MappingProxyType({"mag": "tesla"}),
        description="Magnetometer",
    ),
    StreamDescriptor(
        "barometer",
        "barometer",
        "baro0",
        ("247-1",),
        "baro",
        sdk_callback="barometer",
        profile_key="barometer",
        nominal_rate_hz=50,
        parameters=_params(ParameterSpec("rate_hz", 50, choices=(50,), profile_field="rate_hz")),
        units=MappingProxyType({"pressure": "Pa", "temperature": "degC"}),
        description="Barometer",
    ),
    StreamDescriptor(
        "audio",
        "audio",
        "mic",
        ("231-1",),
        "audio",
        sdk_callback="audio",
        profile_key="audio",
        nominal_rate_hz=50,
        parameters=_params(
            ParameterSpec("sample_rate_hz", 16000, choices=(16000,), profile_field="sample_rate")
        ),
        units=MappingProxyType({"samples": "int16"}),
        description="Microphone array (7+1 channels, 16 kHz, delivered as 20 ms chunks = 50 Hz)",
    ),
    StreamDescriptor(
        "gps",
        "gps",
        "gps",
        ("281-2",),
        "gps",
        sdk_callback="gps",
        profile_key="gps",
        nominal_rate_hz=1,
        parameters=_params(ParameterSpec("rate_hz", 1, choices=(1,), profile_field="rate_hz")),
        units=MappingProxyType({"lat": "deg", "lon": "deg", "alt": "m"}),
        description="On-device GNSS",
    ),
    StreamDescriptor(
        "gps_app",
        "gps",
        "gps-app",
        ("281-1",),
        "gps",
        sdk_callback="phone_location",
        nominal_rate_hz=None,
        description="Companion-app phone location",
    ),
    StreamDescriptor(
        "wifi_beacons",
        "beacon",
        "wps",
        ("282-1",),
        "dict",
        sdk_callback="wifi_beacon",
        profile_key="wifi",
        parameters=_params(
            ParameterSpec("period_ms", 30000, minimum=1000, profile_field="period_ms")
        ),
        description="Wi-Fi scan results",
    ),
    StreamDescriptor(
        "ble_beacons",
        "beacon",
        "bluetooth",
        ("283-1",),
        "dict",
        sdk_callback="bluetooth_beacon",
        profile_key="ble",
        parameters=_params(
            ParameterSpec("period_ms", 30000, minimum=1000, profile_field="period_ms")
        ),
        description="BLE scan results",
    ),
    StreamDescriptor(
        "ppg",
        "bio",
        "ppg",
        ("248-1",),
        "scalar",
        sdk_callback="ppg",
        profile_key="ppg",
        nominal_rate_hz=128,
        parameters=_params(ParameterSpec("rate_hz", 128, choices=(128,), profile_field="rate_hz")),
        units=MappingProxyType({"value": "raw"}),
        description="Photoplethysmography (nose pad)",
    ),
    StreamDescriptor(
        "als",
        "env",
        "als",
        ("500-1",),
        "scalar",
        sdk_callback=None,
        profile_key="als",
        nominal_rate_hz=9.434,
        parameters=_params(
            ParameterSpec("rate_hz", 9.434, choices=(9.434, 10), profile_field="rate_hz")
        ),
        units=MappingProxyType({"value": "raw"}),
        description="Ambient light sensor (VRS only: no typed live callback in SDK 2.5)",
    ),
    StreamDescriptor(
        "temperature",
        "env",
        "temperature",
        ("246-1",),
        "scalar",
        sdk_callback=None,
        profile_key="temperature",
        nominal_rate_hz=1,
        parameters=_params(ParameterSpec("rate_hz", 1, choices=(1,), profile_field="rate_hz")),
        units=MappingProxyType({"value": "degC"}),
        description="Device temperature (VRS only: no typed live callback in SDK 2.5)",
    ),
    StreamDescriptor(
        "eye_gaze",
        "mp",
        "eyegaze",
        ("373-1",),
        "gaze",
        sdk_callback="eye_gaze",
        profile_key="et",
        nominal_rate_hz=30,
        parameters=_params(_MP_RATE_30),
        units=MappingProxyType({"yaw": "rad", "pitch": "rad", "depth": "m"}),
        description="On-device eye gaze (CPF frame)",
    ),
    StreamDescriptor(
        "hand_tracking",
        "mp",
        "handtracking",
        ("371-*",),
        "hand",
        sdk_callback="hand_pose",
        profile_key="ht",
        nominal_rate_hz=30,
        parameters=_params(_MP_RATE_30),
        units=MappingProxyType({"landmarks": "m"}),
        description="On-device hand tracking",
    ),
    StreamDescriptor(
        "vio",
        "mp",
        "vio",
        ("371-*",),
        "pose",
        sdk_callback="vio",
        profile_key="vio",
        nominal_rate_hz=10,
        parameters=_params(_MP_RATE_10),
        units=MappingProxyType({"translation": "m", "velocity": "m/s"}),
        description="On-device visual-inertial odometry (with online calibration)",
    ),
    StreamDescriptor(
        "vio_high_frequency",
        "mp",
        "vio_high_frequency",
        ("371-*",),
        "pose",
        sdk_callback="vio_high_frequency",
        profile_key="vio_high_frequency_pose",
        nominal_rate_hz=800,
        parameters=_params(_RATE_800),
        units=MappingProxyType({"translation": "m"}),
        description="800 Hz open-loop trajectory",
    ),
    StreamDescriptor(
        "device_calibration",
        "calib",
        "calibration",
        (),
        "dict",
        sdk_callback="device_calib",
        description="Factory calibration, delivered once per connection",
    ),
)

_GROUPS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "rgb": ("rgb",),
        "slam": ("slam_front_left", "slam_front_right", "slam_side_left", "slam_side_right"),
        "et": ("et_left", "et_right"),
        "imu": ("imu_left", "imu_right"),
    }
)


class StreamRegistry:
    """Read-only lookup over :data:`_DESCRIPTORS` with group aliases."""

    def __init__(
        self, descriptors: tuple[StreamDescriptor, ...], groups: Mapping[str, tuple[str, ...]]
    ) -> None:
        self._by_id = {d.id: d for d in descriptors}
        self._by_label = {d.native_label: d for d in descriptors}
        self._groups = groups

    def __iter__(self) -> Iterator[StreamDescriptor]:
        return iter(self._by_id.values())

    def __contains__(self, stream_id: str) -> bool:
        return stream_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def ids(self) -> tuple[str, ...]:
        """All stream ids."""
        return tuple(self._by_id)

    def get(self, stream_id: str) -> StreamDescriptor:
        """Return the descriptor or raise with a 'did you mean' hint."""
        try:
            return self._by_id[stream_id]
        except KeyError:
            raise RegistryError(
                f"unknown stream {stream_id!r}", hint=self._suggest(stream_id)
            ) from None

    def by_label(self, native_label: str) -> StreamDescriptor:
        """Lookup by VRS label (``"camera-rgb"``)."""
        try:
            return self._by_label[native_label]
        except KeyError:
            raise RegistryError(f"unknown VRS label {native_label!r}") from None

    def is_group(self, key: str) -> bool:
        """True when ``key`` is an alias for several streams (``"imu"``, ``"slam"``)."""
        return key in self._groups and key not in self._by_id

    def expand(self, key: str) -> tuple[str, ...]:
        """Expand a config key (stream id or group alias) into concrete stream ids."""
        if key in self._by_id:
            return (key,)
        if key in self._groups:
            return self._groups[key]
        raise RegistryError(f"unknown stream or group {key!r}", hint=self._suggest(key))

    def config_keys(self) -> tuple[str, ...]:
        """Keys a user may write under ``streams`` (group aliases + ungrouped stream ids)."""
        grouped = {s for members in self._groups.values() for s in members}
        return tuple(self._groups) + tuple(
            d.id for d in self if d.id not in grouped and d.id != "device_calibration"
        )

    def group_of(self, stream_id: str) -> str | None:
        """The group alias a stream belongs to (``None`` if ungrouped)."""
        return self.get(stream_id).group

    def live_streams(self) -> tuple[str, ...]:
        """Streams available through typed SDK callbacks."""
        return tuple(d.id for d in self if d.live_available)

    def _suggest(self, key: str) -> str:
        import difflib

        candidates = list(dict.fromkeys([*self._by_id, *self._groups]))
        close = difflib.get_close_matches(key, candidates, n=3, cutoff=0.5)
        if close:
            return "did you mean " + " / ".join(repr(c) for c in close) + "?"
        return "run `ariastreamlab streams` to list the available streams"


REGISTRY = StreamRegistry(_DESCRIPTORS, _GROUPS)
"""The process-wide registry."""
