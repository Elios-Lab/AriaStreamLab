"""Timestamped experiment events: user markers, device state changes, config changes, warnings.

Events share the device time base of the sensor streams. When a marker is created on the
host, its device timestamp is estimated from the most recent host↔device observation held by
:class:`DeviceTimeEstimator`; the estimate quality is stored with the event so analysis code
can tell an exact device timestamp (replay) from an estimated one (live marker).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.sample import AnySample, Sample
from ariastreamlab.timing.clock import HostClock

EventKind = Literal["marker", "device_state", "config_change", "warning", "clock_anchor", "session"]


@dataclass(frozen=True, slots=True)
class Event:
    """One event row."""

    kind: EventKind
    name: str
    device_timestamp_ns: int
    host_timestamp_ns: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    timestamp_source: Literal["device", "estimated", "host_only"] = "estimated"
    """How ``device_timestamp_ns`` was obtained."""

    def payload_json(self) -> str:
        """Payload serialised for storage."""
        return json.dumps(self.payload, default=str, sort_keys=True)


class DeviceTimeEstimator:
    """Maps host monotonic time to device time from the samples flowing through the bus.

    The estimate is ``device = host − offset`` where ``offset`` is the minimum ``host − device``
    seen (the sample that travelled fastest), refreshed continuously. It is an upper bound on
    the true device time by the minimum transport latency; a TicSync mapping, when available,
    replaces it (Phase 4).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._offset_ns: int | None = None
        self._samples = 0

    def observe(self, sample: AnySample) -> None:
        """Feed a raw sample."""
        if not isinstance(sample, Sample):
            return
        delta = sample.host_receive_timestamp_ns - sample.device_timestamp_ns
        with self._lock:
            self._samples += 1
            if self._offset_ns is None or delta < self._offset_ns:
                self._offset_ns = delta

    def set_offset(self, host_minus_device_ns: int) -> None:
        """Override with an authoritative offset (TicSync)."""
        with self._lock:
            self._offset_ns = host_minus_device_ns

    def device_from_host(self, host_ns: int) -> tuple[int, Literal["estimated", "host_only"]]:
        """Estimate the device timestamp for a host timestamp."""
        with self._lock:
            if self._offset_ns is None:
                return host_ns, "host_only"
            return host_ns - self._offset_ns, "estimated"

    @property
    def ready(self) -> bool:
        """True once at least one sample has been observed."""
        return self._offset_ns is not None


class EventLog:
    """In-memory event list that also publishes every event on ``events.<kind>``."""

    def __init__(
        self, bus: EventBus, estimator: DeviceTimeEstimator, clock: HostClock | None = None
    ) -> None:
        self._bus = bus
        self._estimator = estimator
        self._clock = clock or HostClock()
        self._lock = threading.Lock()
        self._events: list[Event] = []

    def mark(
        self, name: str, payload: Mapping[str, Any] | None = None, *, kind: EventKind = "marker"
    ) -> Event:
        """Create an event stamped with the current host time (device time estimated)."""
        host_ns = self._clock.now_ns()
        device_ns, how = self._estimator.device_from_host(host_ns)
        return self.add(Event(kind, name, device_ns, host_ns, dict(payload or {}), how))

    def add(self, event: Event) -> Event:
        """Append an already-built event and publish it."""
        with self._lock:
            self._events.append(event)
        self._bus.publish(f"events.{event.kind}", event)
        return event

    def all(self) -> list[Event]:
        """Copy of all events so far."""
        with self._lock:
            return list(self._events)

    def __len__(self) -> int:
        return len(self._events)
