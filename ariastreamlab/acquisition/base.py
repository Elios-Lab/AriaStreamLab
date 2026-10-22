"""Abstract acquisition interfaces.

The rest of AriaStreamLab depends only on these classes (dependency inversion): the Aria Gen 2
adapter, the replay sources and the mock source implement them, and nothing outside
``ariastreamlab.acquisition.aria_gen2`` ever imports Meta's SDK.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import DeviceError
from ariastreamlab.core.sample import Origin


# --------------------------------------------------------------------------- sources
@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    """What a source can do."""

    origin: Origin
    streams: tuple[str, ...]
    """Stream ids this source will publish on ``raw.<id>``."""
    can_record_raw: bool
    """True when the source itself writes the raw ground truth (the SDK's VRS writer)."""
    is_paced: bool
    """True when samples arrive in (approximately) real time."""
    supports_replay_control: bool = False


@dataclass(frozen=True, slots=True)
class SourceStats:
    """Counters a source knows about itself (what the bus cannot see)."""

    received: Mapping[str, int]
    """Samples handed to the bus, per stream."""
    source_dropped: Mapping[str, int]
    """Samples the source knows it lost before the bus (SDK queue drops, simulated drops)."""
    extra: Mapping[str, Any] = field(default_factory=dict)


class Source(ABC):
    """Produces :class:`~ariastreamlab.core.sample.Sample` objects on an :class:`EventBus`.

    Lifecycle: ``start(bus)`` → samples flow on ``raw.<stream_id>`` → ``stop()``.
    ``start`` must return quickly (spawn threads, register callbacks); it must never block
    for the duration of the experiment.
    """

    capabilities: SourceCapabilities

    @abstractmethod
    def start(self, bus: EventBus) -> None:
        """Begin publishing."""

    @abstractmethod
    def stop(self) -> None:
        """Stop publishing and release resources. Idempotent."""

    @abstractmethod
    def stats(self) -> SourceStats:
        """Current counters."""

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the source has finished on its own (replay/mock end) or ``timeout``.

        Returns ``True`` when finished. Live sources never finish by themselves.
        """
        return self.finished.wait(timeout)

    @property
    def finished(self) -> threading.Event:
        """Set when the source will produce no more samples."""
        ev = getattr(self, "_finished_event", None)
        if ev is None:
            ev = threading.Event()
            self._finished_event = ev
        return ev


class ReplayControl(Protocol):
    """Extra controls implemented by replay sources."""

    def pause(self) -> None: ...  # noqa: D102
    def resume(self) -> None: ...  # noqa: D102
    def seek(self, device_timestamp_ns: int) -> None: ...  # noqa: D102
    def set_speed(self, multiplier: float) -> None: ...  # noqa: D102
    @property
    def paused(self) -> bool: ...  # noqa: D102


# --------------------------------------------------------------------------- devices
class DeviceState(str, Enum):
    """Connection state machine (docs/ARCHITECTURE.md §4.3)."""

    DISCONNECTED = "DISCONNECTED"
    DISCOVERED = "DISCOVERED"
    CONNECTED = "CONNECTED"
    NETWORK_CONFIGURED = "NETWORK_CONFIGURED"
    STREAM_READY = "STREAM_READY"
    STREAMING = "STREAMING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


_ALLOWED: Mapping[DeviceState, frozenset[DeviceState]] = {
    DeviceState.DISCONNECTED: frozenset(
        {DeviceState.DISCOVERED, DeviceState.CONNECTED, DeviceState.ERROR}
    ),
    DeviceState.DISCOVERED: frozenset(
        {DeviceState.CONNECTED, DeviceState.DISCONNECTED, DeviceState.ERROR}
    ),
    DeviceState.CONNECTED: frozenset(
        {
            DeviceState.NETWORK_CONFIGURED,
            DeviceState.STREAM_READY,
            DeviceState.DISCONNECTED,
            DeviceState.ERROR,
        }
    ),
    DeviceState.NETWORK_CONFIGURED: frozenset(
        {
            DeviceState.STREAM_READY,
            DeviceState.CONNECTED,
            DeviceState.DISCONNECTED,
            DeviceState.ERROR,
        }
    ),
    DeviceState.STREAM_READY: frozenset(
        {DeviceState.STREAMING, DeviceState.CONNECTED, DeviceState.DISCONNECTED, DeviceState.ERROR}
    ),
    DeviceState.STREAMING: frozenset({DeviceState.STOPPING, DeviceState.ERROR}),
    DeviceState.STOPPING: frozenset(
        {DeviceState.CONNECTED, DeviceState.DISCONNECTED, DeviceState.ERROR}
    ),
    DeviceState.ERROR: frozenset({DeviceState.DISCONNECTED, DeviceState.CONNECTED}),
}


@dataclass(frozen=True, slots=True)
class DeviceTransition:
    """One state change, published on ``events.device_state``."""

    previous: DeviceState
    current: DeviceState
    host_timestamp_ns: int
    reason: str = ""
    transport: str | None = None


class DeviceStateMachine:
    """Thread-safe state holder with transition validation and listeners."""

    def __init__(self, initial: DeviceState = DeviceState.DISCONNECTED) -> None:
        self._state = initial
        self._lock = threading.Lock()
        self._listeners: list[Callable[[DeviceTransition], None]] = []
        self.history: list[DeviceTransition] = []
        self.last_error: DeviceError | None = None

    @property
    def state(self) -> DeviceState:
        """Current state."""
        return self._state

    def on_transition(self, fn: Callable[[DeviceTransition], None]) -> None:
        """Register a listener."""
        self._listeners.append(fn)

    def transition(
        self, new: DeviceState, *, reason: str = "", transport: str | None = None, now_ns: int
    ) -> DeviceTransition:
        """Move to ``new``; raises :class:`DeviceError` for an illegal transition."""
        with self._lock:
            if new not in _ALLOWED[self._state] and new is not self._state:
                raise DeviceError(
                    f"illegal device state transition {self._state.value} -> {new.value}"
                )
            t = DeviceTransition(self._state, new, now_ns, reason, transport)
            self._state = new
            self.history.append(t)
        for fn in self._listeners:
            fn(t)
        return t

    def fail(
        self, error: DeviceError, *, now_ns: int, transport: str | None = None
    ) -> DeviceTransition:
        """Record an error and move to ``ERROR``."""
        self.last_error = error
        return self.transition(
            DeviceState.ERROR, reason=str(error), transport=transport, now_ns=now_ns
        )


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """A discovered device."""

    serial: str
    transport: str
    """``"usb"``, ``"wifi"`` or ``"ble"``."""
    ip: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeviceHealth:
    """Device-side health snapshot (subset of the SDK ``DeviceStatus``, SDK-independent)."""

    battery_percent: int | None = None
    charging: bool | None = None
    skin_temperature_c: float | None = None
    thermal_mitigation: bool | None = None
    wifi_connected: bool | None = None
    wifi_ssid: str | None = None
    wifi_ip: str | None = None
    device_mode: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HotspotInfo:
    """Credentials of the glasses' hotspot once started."""

    ssid: str | None
    password: str | None
    device_ip: str | None
    raw_output: str = ""


@dataclass(frozen=True, slots=True)
class StreamingPlan:
    """Everything the adapter needs to start streaming, derived from the experiment config."""

    streams: tuple[str, ...]
    interface: str
    batch_period_ms: int
    profile_name: str | None
    profile_json: str | None
    record_vrs_path: str | None
    server_port: int
    warnings: tuple[str, ...] = ()
    expected_rates_hz: Mapping[str, float] = field(default_factory=dict)
    """Rate each live stream should deliver: the measured profile rate where known, else the
    configured / nominal one. The quality summary judges streams against this."""


class DeviceManager(ABC):
    """Control-plane operations on a device (docs/ARCHITECTURE.md §4.3)."""

    machine: DeviceStateMachine

    @property
    def state(self) -> DeviceState:
        """Current state."""
        return self.machine.state

    @abstractmethod
    def discover(self) -> list[DeviceInfo]:
        """List reachable devices."""

    @abstractmethod
    def connect(self, target: DeviceInfo | None = None) -> None:
        """Connect to ``target`` or the first device found."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release the device."""

    @abstractmethod
    def get_status(self) -> DeviceHealth:
        """Battery, temperature, Wi-Fi…."""

    @abstractmethod
    def prepare_hotspot(self) -> HotspotInfo:
        """Start the device hotspot and (re)connect over it."""

    @abstractmethod
    def configure_streaming(self, plan: StreamingPlan) -> None:
        """Push the streaming configuration; state becomes ``STREAM_READY``."""

    @abstractmethod
    def start_streaming(self) -> None:
        """State becomes ``STREAMING``."""

    @abstractmethod
    def stop_streaming(self) -> None:
        """State goes ``STOPPING`` → ``CONNECTED``."""

    @abstractmethod
    def quality_stats(self) -> Mapping[str, Any] | None:
        """Device-side data-quality statistics, if the device provides them now."""
