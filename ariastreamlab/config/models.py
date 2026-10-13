"""Pydantic models of the experiment configuration (docs/ARCHITECTURE.md §5).

Every model uses ``extra="forbid"`` so a typo (``"procesors"``) is an error, not a silently
ignored key. The only exception is :class:`StreamConfig`, whose extra keys are the stream
parameters validated against :data:`ariastreamlab.core.registry.REGISTRY`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ariastreamlab.core.bus import DropPolicy
from ariastreamlab.core.errors import ConfigError, RegistryError
from ariastreamlab.core.registry import REGISTRY

SCHEMA_VERSION = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, use_enum_values=False)


# --------------------------------------------------------------------------- experiment
class ExperimentInfo(_Strict):
    """Who / what is being recorded."""

    name: str = Field(
        min_length=1, pattern=r"^[A-Za-z0-9_\-]+$", description="Used in the session folder name"
    )
    participant: str = Field(default="P000", pattern=r"^[A-Za-z0-9_\-]+$")
    description: str = ""
    tags: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- device
class HotspotConfig(_Strict):
    """Parameters of the glasses' Wi-Fi hotspot (``aria_gen2 device hotspot start``)."""

    band: Literal["2.4GHz", "5GHz", "6GHz"] = "5GHz"
    bandwidth: Literal["20MHz", "40MHz", "80MHz", "160MHz"] = "80MHz"
    auto_join: bool = Field(
        default=False,
        description="macOS only: join the hotspot with `networksetup` instead of asking the user",
    )


class ConnectionConfig(_Strict):
    """How the laptop talks to the glasses."""

    preferred_control: Literal["auto", "usb", "wifi", "ble"] = "auto"
    streaming_interface: Literal["usb", "wifi_sta", "wifi_sap"] = "wifi_sap"
    batch_period_ms: int | None = Field(
        default=None,
        ge=0,
        le=5000,
        description="null ⇒ 0 for usb, 200 for wifi_sta, 800 for wifi_sap (Meta's recommendations)",
    )
    hotspot: HotspotConfig = Field(default_factory=HotspotConfig)
    reconnect_attempts: int = Field(default=2, ge=0, le=20)
    device_ip: str | None = Field(default=None, description="Skip discovery and connect to this IP")
    server_port: int = Field(default=6768, ge=1024, le=65535)

    @property
    def effective_batch_period_ms(self) -> int:
        """Batch period actually sent to the device."""
        if self.batch_period_ms is not None:
            return self.batch_period_ms
        return {"usb": 0, "wifi_sta": 200, "wifi_sap": 800}[self.streaming_interface]


class ProfileConfig(_Strict):
    """Which Aria streaming profile is used.

    ``predefined``: pick the closest of Meta's tested profiles for the requested streams
    (default, stable). ``custom``: generate a custom profile JSON from the streams section.
    ``named``: use ``name`` verbatim (streams section only selects callbacks).
    """

    mode: Literal["predefined", "custom", "named"] = "predefined"
    name: str | None = None
    custom_json: Path | None = None

    @model_validator(mode="after")
    def _check(self) -> ProfileConfig:
        if self.mode == "named" and not self.name:
            raise ValueError("device.profile.name is required when mode is 'named'")
        return self


class DeviceConfig(_Strict):
    """Device section."""

    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    serial: str | None = None


# --------------------------------------------------------------------------- streams
class StreamConfig(BaseModel):
    """``enabled`` plus the parameters allowed by the registry for that stream."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True

    def parameters(self) -> dict[str, Any]:
        """Extra keys (the stream parameters as written by the user)."""
        return dict(self.model_extra or {})


# --------------------------------------------------------------------------- processors
class ExecutionConfig(_Strict):
    """How a processor is scheduled."""

    queue_size: int = Field(default=4, ge=1, le=10_000)
    drop_policy: DropPolicy = DropPolicy.DROP_OLDEST
    join: Literal["latest", "nearest", "previous"] = Field(
        default="latest",
        description="Multi-input processors: how secondary inputs match the primary (first) input",
    )
    tolerance_ms: float = Field(default=50.0, ge=0)
    isolate: Literal["thread", "process"] = Field(
        default="thread", description="'process' is reserved for a later phase"
    )

    @field_validator("drop_policy")
    @classmethod
    def _no_block(cls, v: DropPolicy) -> DropPolicy:
        if v is DropPolicy.BLOCK:
            raise ValueError(
                "'block' would stall the acquisition path; use drop_oldest, drop_newest or latest"
            )
        return v


class ProcessorConfig(_Strict):
    """One node of the processing graph."""

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    type: str = Field(
        min_length=1,
        description="Built-in name (image.resize), 'module:Class' or a file in processors_path",
    )
    inputs: list[str] = Field(min_length=1)
    outputs: list[str] = Field(min_length=1)
    enabled: bool = True
    parameters: dict[str, Any] = Field(default_factory=dict)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    save_output: bool = True

    @field_validator("outputs")
    @classmethod
    def _outputs_unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("outputs must be unique")
        for o in v:
            if o in REGISTRY:
                raise ValueError(
                    f"output {o!r} clashes with a raw stream id; derived streams need own names"
                )
        return v


# --------------------------------------------------------------------------- other sections
class DashboardConfig(_Strict):
    """Live dashboard."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    open_browser: bool = True
    show: list[str] | None = Field(
        default=None, description="Streams / derived streams to display; null ⇒ all enabled"
    )
    image_fps: float = Field(default=10.0, gt=0, le=60)
    rerun: bool = Field(
        default=False, description="Also log to a Rerun viewer (requires the 'rerun' extra)"
    )


class StorageConfig(_Strict):
    """Where and how a session is written."""

    root: Path = Path("./sessions")
    save_raw_vrs: bool = True
    chunk_seconds: float = Field(default=60.0, gt=0)
    image_format: Literal["mp4", "jpeg", "png"] = "mp4"
    jpeg_quality: int = Field(default=90, ge=1, le=100)
    save_mock_raw: bool = Field(
        default=True,
        description="Mock mode: write the synthetic raw streams as derived-style files",
    )


class ReplayConfig(_Strict):
    """Replay options (also settable from the CLI)."""

    speed: float = Field(default=1.0, ge=0, description="0 ⇒ as fast as possible")
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, ge=0)
    backend: Literal["tools", "sdk"] = Field(
        default="tools",
        description="tools: projectaria_tools data provider, exact and seekable; "
        "sdk: the client SDK replay engine, same callbacks and bounded queues as a live device",
    )


class LoggingConfig(_Strict):
    """Logging."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["text", "json"] = Field(
        default="text", description="'json' writes JSON lines (one object per line)"
    )


class MockConfig(_Strict):
    """Synthetic source used by ``--mock`` and by the tests."""

    seed: int = 0
    jitter_ms: float = Field(default=0.1, ge=0, description="Gaussian jitter on device timestamps")
    drop_probability: float = Field(
        default=0.0, ge=0, le=0.9, description="Simulated transport drops"
    )
    out_of_order_probability: float = Field(default=0.0, ge=0, le=0.5)
    image_scale: float = Field(
        default=0.25,
        gt=0,
        le=1,
        description="Mock cameras render at this fraction of the configured resolution",
    )
    realtime: bool = Field(default=True, description="False ⇒ generate as fast as possible (tests)")


# --------------------------------------------------------------------------- root
class ExperimentConfig(_Strict):
    """The whole experiment, i.e. the one JSON file a student edits."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str | None = Field(default=None, alias="$schema")
    schema_version: int = SCHEMA_VERSION
    extends: str | None = Field(
        default=None,
        description="'preset:<name>' or a path relative to this file; resolved by the loader",
    )
    experiment: ExperimentInfo
    device: DeviceConfig = Field(default_factory=DeviceConfig)
    streams: dict[str, StreamConfig] = Field(default_factory=dict)
    processors: list[ProcessorConfig] = Field(default_factory=list)
    processors_path: Path | None = None
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    mock: MockConfig = Field(default_factory=MockConfig)

    # -- derived views --------------------------------------------------------------
    def enabled_streams(self) -> dict[str, dict[str, Any]]:
        """Concrete enabled stream ids → effective parameters (defaults applied)."""
        out: dict[str, dict[str, Any]] = {}
        for key, cfg in self.streams.items():
            if not cfg.enabled:
                continue
            for sid in REGISTRY.expand(key):
                desc = REGISTRY.get(sid)
                params = {name: spec.default for name, spec in desc.parameters.items()}
                params.update(cfg.parameters())
                out[sid] = params
        return out

    def derived_streams(self) -> dict[str, ProcessorConfig]:
        """Derived stream id → the processor that produces it."""
        return {o: p for p in self.processors if p.enabled for o in p.outputs}

    def all_stream_ids(self) -> tuple[str, ...]:
        """Enabled raw streams followed by derived streams."""
        return tuple(self.enabled_streams()) + tuple(self.derived_streams())

    # -- cross-field validation --------------------------------------------------------
    @model_validator(mode="after")
    def _validate(self) -> ExperimentConfig:
        self._validate_streams()
        self._validate_processors()
        self._validate_dashboard()
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version {self.schema_version} is not supported"
                f" (this version reads {SCHEMA_VERSION})"
            )
        return self

    def _validate_streams(self) -> None:
        seen: dict[str, str] = {}
        for key, cfg in self.streams.items():
            if key == "device_calibration":
                raise ValueError(
                    "streams.device_calibration: calibration is always captured; remove this key"
                )
            try:
                members = REGISTRY.expand(key)
            except RegistryError as exc:
                raise ValueError(str(exc)) from None
            for sid in members:
                if sid in seen:
                    raise ValueError(
                        f"streams: {sid!r} is configured twice (via {seen[sid]!r} and {key!r})"
                    )
                seen[sid] = key
            spec_owner = REGISTRY.get(members[0])
            for pname, value in cfg.parameters().items():
                spec = spec_owner.parameters.get(pname)
                if spec is None:
                    allowed = ", ".join(spec_owner.parameters) or "none"
                    raise ValueError(
                        f"streams.{key}.{pname}: unknown parameter for this stream"
                        f" (allowed: {allowed})"
                    )
                try:
                    spec.validate(value, key)
                except RegistryError as exc:
                    raise ValueError(str(exc)) from None

    def _validate_processors(self) -> None:
        ids = [p.id for p in self.processors]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"processors: duplicate id(s) {sorted(dup)}")
        raw = set(self.enabled_streams())
        producers: dict[str, str] = {}
        for p in self.processors:
            if not p.enabled:
                continue
            for o in p.outputs:
                if o in producers:
                    raise ValueError(
                        f"processors: output {o!r} is produced by both"
                        f" {producers[o]!r} and {p.id!r}"
                    )
                producers[o] = p.id
        # inputs must exist; detect cycles with a DFS over processor dependencies
        deps: dict[str, set[str]] = {}
        for p in self.processors:
            if not p.enabled:
                continue
            deps[p.id] = set()
            for i in p.inputs:
                if i in raw:
                    continue
                if i in producers:
                    deps[p.id].add(producers[i])
                    continue
                if i in REGISTRY:
                    raise ValueError(
                        f"processors[{p.id}].inputs: stream {i!r} exists"
                        " but is not enabled in 'streams'"
                    )
                raise ValueError(
                    f"processors[{p.id}].inputs: unknown stream {i!r}"
                    " (not a raw stream, not an output of another processor)"
                )
        state: dict[str, int] = {}

        def visit(node: str, path: list[str]) -> None:
            if state.get(node) == 1:
                raise ValueError(f"processors: cycle detected: {' -> '.join([*path, node])}")
            if state.get(node) == 2:
                return
            state[node] = 1
            for d in deps[node]:
                visit(d, [*path, node])
            state[node] = 2

        for pid in deps:
            visit(pid, [])

    def _validate_dashboard(self) -> None:
        if self.dashboard.show is None:
            return
        known = set(self.all_stream_ids())
        for s in self.dashboard.show:
            if s not in known:
                raise ValueError(
                    f"dashboard.show: {s!r} is neither an enabled stream nor a processor output"
                )


def format_validation_error(exc: Exception, path: Path | None = None) -> ConfigError:
    """Turn a pydantic ``ValidationError`` into a :class:`ConfigError` a student can act on."""
    from pydantic import ValidationError

    where = f" in {path}" if path else ""
    if not isinstance(exc, ValidationError):
        return ConfigError(f"invalid configuration{where}: {exc}")
    lines = [f"invalid configuration{where} ({exc.error_count()} problem(s)):"]
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "<root>"
        msg = err["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        lines.append(f"  - {loc}: {msg}")
    return ConfigError(
        "\n".join(lines), hint="run `ariastreamlab inspect-config <file>` after fixing"
    )
