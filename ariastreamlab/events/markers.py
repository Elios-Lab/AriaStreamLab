"""Timestamped experiment events: user markers, device state changes, config changes, warnings.

Events share the device time base of the sensor streams. When a marker is created on the
host, its device timestamp is estimated from the most recent host↔device observation held by
:class:`DeviceTimeEstimator`; the estimate quality is stored with the event so analysis code
can tell an exact device timestamp (replay) from an estimated one (live marker).
"""

from __future__ import annotations

import dataclasses
import json
import threading
from collections.abc import Iterable, Mapping
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


@dataclass(frozen=True, slots=True)
class Interval:
    """A ``begin``/``end`` pair of marker events (a trial, a condition, a stimulus…)."""

    name: str
    interval_id: str
    start_ns: int
    end_ns: int | None
    payload: Mapping[str, Any]
    """Start payload updated with the end payload (``phase``/``interval`` keys removed)."""
    start_source: str = "estimated"
    end_source: str | None = None

    @property
    def duration_s(self) -> float | None:
        """Length in seconds (``None`` while open)."""
        return None if self.end_ns is None else (self.end_ns - self.start_ns) / 1e9


def pair_intervals(events: Iterable[Event], name: str | None = None) -> list[Interval]:
    """Match ``begin`` and ``end`` marker events (by ``payload["interval"]``) into intervals."""
    starts: dict[str, Event] = {}
    out: list[Interval] = []
    for e in events:
        if e.kind != "marker" or not isinstance(e.payload, Mapping):
            continue
        phase = e.payload.get("phase")
        iid = e.payload.get("interval")
        if phase not in ("start", "end") or not isinstance(iid, str):
            continue
        if name is not None and e.name != name:
            continue
        if phase == "start":
            starts[iid] = e
            continue
        start = starts.pop(iid, None)
        if start is None:
            continue  # an end without a start (e.g. a crash before the marker landed)
        payload = {k: v for k, v in start.payload.items() if k not in ("phase", "interval")}
        payload.update({k: v for k, v in e.payload.items() if k not in ("phase", "interval")})
        out.append(
            Interval(
                start.name,
                iid,
                start.device_timestamp_ns,
                e.device_timestamp_ns,
                payload,
                start.timestamp_source,
                e.timestamp_source,
            )
        )
    for iid, start in starts.items():  # still open
        payload = {k: v for k, v in start.payload.items() if k not in ("phase", "interval")}
        out.append(
            Interval(
                start.name, iid, start.device_timestamp_ns, None, payload, start.timestamp_source
            )
        )
    out.sort(key=lambda i: i.start_ns)
    return out


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

    @property
    def offset_ns(self) -> int | None:
        """Current ``host − device`` estimate (``None`` before any sample)."""
        with self._lock:
            return self._offset_ns


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
        self._open: dict[str, str] = {}
        self._interval_seq = 0

    def mark(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        kind: EventKind = "marker",
        at_ns: int | None = None,
    ) -> Event:
        """Create an event stamped *now* (device time estimated) or at an explicit device time.

        ``at_ns`` is a device timestamp — typically the ``device_timestamp_ns`` of the sample
        the annotation refers to — and makes the event exact (``timestamp_source="device"``).
        """
        host_ns = self._clock.now_ns()
        if at_ns is not None:
            return self.add(Event(kind, name, int(at_ns), host_ns, dict(payload or {}), "device"))
        device_ns, how = self._estimator.device_from_host(host_ns)
        return self.add(Event(kind, name, device_ns, host_ns, dict(payload or {}), how))

    def begin(
        self, name: str, payload: Mapping[str, Any] | None = None, *, at_ns: int | None = None
    ) -> str:
        """Open an interval (a trial, a condition…); returns its id for :meth:`end`."""
        with self._lock:
            self._interval_seq += 1
            iid = f"{name}-{self._interval_seq}"
            self._open[iid] = name
        self.mark(name, {**dict(payload or {}), "phase": "start", "interval": iid}, at_ns=at_ns)
        return iid

    def end(
        self,
        name_or_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        at_ns: int | None = None,
    ) -> str:
        """Close an interval by id, or the most recently opened one with that name."""
        with self._lock:
            if name_or_id in self._open:
                iid = name_or_id
            else:
                candidates = [i for i, n in self._open.items() if n == name_or_id]
                if not candidates:
                    raise ValueError(f"no open interval named {name_or_id!r}")
                iid = candidates[-1]
            name = self._open.pop(iid)
        self.mark(name, {**dict(payload or {}), "phase": "end", "interval": iid}, at_ns=at_ns)
        return iid

    def open_intervals(self) -> dict[str, str]:
        """``interval id → name`` of the intervals not yet ended."""
        with self._lock:
            return dict(self._open)

    def intervals(self, name: str | None = None) -> list[Interval]:
        """Paired intervals so far (open ones have ``end_ns=None``)."""
        return pair_intervals(self.all(), name)

    def add(self, event: Event) -> Event:
        """Append an already-built event and publish it."""
        with self._lock:
            self._events.append(event)
        self._bus.publish(f"events.{event.kind}", event)
        return event

    def all(self, *, resolve: bool = True) -> list[Event]:
        """Copy of all events so far.

        With ``resolve`` (the default) events stamped before any sample arrived
        (``timestamp_source == "host_only"``, e.g. the device state changes during
        connection) get their device timestamp estimated with the offset known now, so the
        stored events sort correctly against the sensor data.
        """
        with self._lock:
            events = list(self._events)
        if not resolve or not self._estimator.ready:
            return events
        out: list[Event] = []
        for e in events:
            if e.timestamp_source == "host_only":
                device_ns, how = self._estimator.device_from_host(e.host_timestamp_ns)
                e = dataclasses.replace(e, device_timestamp_ns=device_ns, timestamp_source=how)
            out.append(e)
        return out

    def __len__(self) -> int:
        return len(self._events)
