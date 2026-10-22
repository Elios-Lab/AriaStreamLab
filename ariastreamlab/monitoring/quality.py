"""The end-of-session quality summary (``quality/summary.json`` + ``summary.txt``).

One place that puts the four kinds of loss side by side, per stream — **device** drops (the
glasses' own counters), **transport** drops (sequence gaps seen by the host), **application
queue** drops (bus subscribers that fell behind) and **processor** drops (per-input queues of
the processing graph) — next to the effective rate, and closes with the session's duration,
disk usage and a list of warnings worth a researcher's attention. Built from plain data so it
can be produced by the live session, by a replay, or later from a manifest.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["QualitySummary", "build_summary", "format_bytes"]


def format_bytes(n: float | None) -> str:
    """``1.84 GB``-style formatting."""
    if n is None:
        return "–"
    value = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1000
    return f"{value:.2f} TB"


@dataclass
class StreamQuality:
    """One row of the summary."""

    stream_id: str
    kind: str  # raw | derived
    received: int = 0
    nominal_rate_hz: float | None = None
    effective_rate_hz: float | None = None
    device_dropped: int | None = None
    device_expected: int | None = None
    device_shared: bool = False  # the device counter covers more than this stream (IMU_1/2)
    transport_dropped: int = 0
    out_of_order: int = 0
    processor_dropped: int = 0
    stored_samples: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class QualitySummary:
    """The whole summary."""

    streams: list[StreamQuality] = field(default_factory=list)
    app_queues: dict[str, dict[str, int]] = field(default_factory=dict)
    processors: dict[str, dict[str, Any]] = field(default_factory=dict)
    duration_s: float | None = None
    disk_bytes: int | None = None
    disk_free_bytes: int | None = None
    peak_write_rate_bps: float | None = None
    device: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:  # noqa: D102
        return {
            "streams": {s.stream_id: s.as_dict() for s in self.streams},
            "app_queues": self.app_queues,
            "processors": self.processors,
            "session": {
                "duration_s": self.duration_s,
                "disk_bytes": self.disk_bytes,
                "disk_free_bytes": self.disk_free_bytes,
                "peak_write_rate_bps": self.peak_write_rate_bps,
                **self.device,
            },
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QualitySummary:
        """Rebuild from :meth:`as_dict` (e.g. the manifest's ``quality`` section)."""
        session = dict(data.get("session") or {})
        known = {"duration_s", "disk_bytes", "disk_free_bytes", "peak_write_rate_bps"}
        out = cls(
            streams=[
                StreamQuality(
                    **{k: v for k, v in row.items() if k in StreamQuality.__dataclass_fields__}
                )
                for row in (data.get("streams") or {}).values()
            ],
            app_queues={k: dict(v) for k, v in (data.get("app_queues") or {}).items()},
            processors={k: dict(v) for k, v in (data.get("processors") or {}).items()},
            duration_s=session.get("duration_s"),
            disk_bytes=session.get("disk_bytes"),
            disk_free_bytes=session.get("disk_free_bytes"),
            peak_write_rate_bps=session.get("peak_write_rate_bps"),
            device={k: v for k, v in session.items() if k not in known},
            warnings=list(data.get("warnings") or []),
        )
        return out

    def text(self) -> str:
        """The concise, aligned text version (what ``run`` prints at the end)."""
        rows: list[list[str]] = []
        for s in self.streams:
            eff = "–" if s.effective_rate_hz is None else f"{s.effective_rate_hz:.2f} Hz"
            nom = "" if s.nominal_rate_hz is None else f" (nominal {s.nominal_rate_hz:g})"
            dev = (
                "–"
                if s.device_dropped is None
                else f"{s.device_dropped}{' (shared)' if s.device_shared else ''}"
            )
            rows.append(
                [
                    s.stream_id,
                    f"received {s.received}",
                    f"device-dropped {dev}" if s.kind == "raw" else "",
                    f"transport-dropped {s.transport_dropped}" if s.kind == "raw" else "",
                    f"processor-dropped {s.processor_dropped}",
                    f"effective {eff}{nom}",
                ]
            )
        widths = [max((len(r[i]) for r in rows), default=0) for i in range(6)]
        lines = ["   ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]
        if self.app_queues:
            dropped = {k: v["dropped"] for k, v in self.app_queues.items() if v.get("dropped")}
            lines.append(
                "app queues   "
                + (
                    ", ".join(f"{k} dropped {v}" for k, v in dropped.items())
                    if dropped
                    else "no drops"
                )
            )
        if self.processors:
            parts = []
            for pid, p in self.processors.items():
                extra = ""
                if p.get("errors"):
                    extra += f" errors {p['errors']}"
                if p.get("exec_ms_p95") is not None:
                    extra += f" p95 {p['exec_ms_p95']:.2f} ms"
                parts.append(f"{pid} {p.get('fired', 0)}→{p.get('outputs', 0)}{extra}")
            lines.append("processors   " + "; ".join(parts))
        dur = "–" if self.duration_s is None else f"{self.duration_s:.1f} s"
        disk = format_bytes(self.disk_bytes)
        free = (
            "" if self.disk_free_bytes is None else f" (free {format_bytes(self.disk_free_bytes)})"
        )
        dev = ""
        if self.device:
            bits = []
            if self.device.get("battery_percent_end") is not None:
                bits.append(
                    f"battery {self.device.get('battery_percent_start')}→"
                    f"{self.device.get('battery_percent_end')} %"
                )
            if self.device.get("skin_temperature_c_max") is not None:
                bits.append(f"skin temp max {self.device['skin_temperature_c_max']:.1f} °C")
            if bits:
                dev = "   " + "   ".join(bits)
        lines.append(
            f"session      duration {dur}   disk {disk}{free}{dev}   warnings: {len(self.warnings)}"
        )
        lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def build_summary(
    *,
    stream_stats: Mapping[str, Any],
    raw_stream_ids: Mapping[str, Any] | set[str] | tuple[str, ...],
    bus_subscriptions: Mapping[str, Mapping[str, int]] | None = None,
    processor_stats: Mapping[str, Mapping[str, Any]] | None = None,
    device_quality: Mapping[str, Any] | None = None,
    device_history: Sequence[Mapping[str, Any]] | None = None,
    recorder_stats: Mapping[str, Any] | None = None,
    duration_s: float | None = None,
    disk_bytes: int | None = None,
    disk_free_bytes: int | None = None,
    peak_write_rate_bps: float | None = None,
    extra_warnings: list[str] | None = None,
) -> QualitySummary:
    """Put the per-stream counters, the device's own statistics and the health history together.

    Args:
        stream_stats: ``StatsTracker`` snapshots (objects or dicts with ``received``,
            ``nominal_rate_hz``, ``effective_rate_hz``, ``transport_dropped``, ``out_of_order``).
        raw_stream_ids: Which stream ids are raw (registry ids); the rest are derived.
        bus_subscriptions: ``{name: {"enqueued", "delivered", "dropped"}}`` from the bus.
        processor_stats: ``ProcessorStats.as_dict()`` per processor id.
        device_quality: The last parsed ``get_data_quality_stats()`` (``QualityStats.as_dict()``).
        device_history: ``HealthLog.device_history``.
        recorder_stats: ``{stream_id: {"samples": n, …}}`` from the derived recorder.
        duration_s: Wall-clock length of the session.
        disk_bytes: Size of the session directory at the end.
        disk_free_bytes: Free space on the disk holding it.
        peak_write_rate_bps: Highest write rate seen by the health log.
        extra_warnings: Warnings collected elsewhere (source, recorder) to repeat here.
    """
    summary = QualitySummary(
        duration_s=duration_s,
        disk_bytes=disk_bytes,
        disk_free_bytes=disk_free_bytes,
        peak_write_rate_bps=peak_write_rate_bps,
    )
    raw_ids = set(raw_stream_ids)
    # device-side counters, attributed to our stream ids
    device_rows: dict[str, tuple[int | None, int | None, bool]] = {}
    for sensor in (device_quality or {}).get("sensors", []) or []:
        streams = list(sensor.get("streams") or [])
        for sid in streams:
            prev = device_rows.get(sid)
            dropped = sensor.get("dropped")
            expected = sensor.get("expected")
            if prev is not None:
                dropped = _add(prev[0], dropped)
                expected = _add(prev[1], expected)
            device_rows[sid] = (dropped, expected, len(streams) > 1 or prev is not None)
    # processor queue drops per input stream
    proc_dropped: dict[str, int] = {}
    for p in (processor_stats or {}).values():
        for sid, n in (p.get("queue_dropped") or {}).items():
            proc_dropped[sid] = proc_dropped.get(sid, 0) + int(n)

    for sid, st in stream_stats.items():
        g = _getter(st)
        dev = device_rows.get(sid)
        rec = (recorder_stats or {}).get(sid)
        row = StreamQuality(
            stream_id=sid,
            kind="raw" if sid in raw_ids else "derived",
            received=int(g("received") or 0),
            nominal_rate_hz=g("nominal_rate_hz"),
            effective_rate_hz=g("effective_rate_hz"),
            device_dropped=None if dev is None else dev[0],
            device_expected=None if dev is None else dev[1],
            device_shared=bool(dev and dev[2]),
            transport_dropped=int(g("transport_dropped") or 0),
            out_of_order=int(g("out_of_order") or 0),
            processor_dropped=proc_dropped.get(sid, 0),
            stored_samples=None if rec is None else _getter(rec)("samples"),
        )
        summary.streams.append(row)
    summary.streams.sort(key=lambda r: (r.kind != "raw", r.stream_id))

    if bus_subscriptions:
        summary.app_queues = {
            name: {
                "enqueued": int(v.get("enqueued", 0)),
                "delivered": int(v.get("delivered", 0)),
                "dropped": int(v.get("dropped", 0)),
            }
            for name, v in bus_subscriptions.items()
        }
    if processor_stats:
        summary.processors = {
            pid: {
                k: p.get(k)
                for k in (
                    "fired",
                    "outputs",
                    "errors",
                    "join_misses",
                    "exec_ms_p95",
                    "latency_ms_p95",
                    "queue_dropped",
                )
            }
            for pid, p in processor_stats.items()
        }
    # device health over the session
    hist = [dict(h) for h in (device_history or [])]
    if hist:
        temps = [h["skin_temperature_c"] for h in hist if h.get("skin_temperature_c") is not None]
        batt = [h["battery_percent"] for h in hist if h.get("battery_percent") is not None]
        summary.device = {
            "battery_percent_start": batt[0] if batt else None,
            "battery_percent_end": batt[-1] if batt else None,
            "skin_temperature_c_max": max(temps) if temps else None,
            "health_snapshots": len(hist),
        }
        mitigated = [h for h in hist if h.get("thermal_mitigation")]
        if mitigated:
            summary.warnings.append(
                f"thermal mitigation active from {mitigated[0]['t_s']:.0f} s"
                f" ({len(mitigated)} of {len(hist)} snapshots)"
            )
        elif temps and max(temps) >= 40:
            summary.warnings.append(
                f"skin temperature reached {max(temps):.1f} °C (throttle at 42)"
            )
        if batt and batt[-1] <= 15:
            summary.warnings.append(f"battery at {batt[-1]} % at the end")
        if any(h.get("wifi_connected") is False for h in hist[1:]):
            summary.warnings.append("wifi reported disconnected during the session")

    # stream-level warnings
    for row in summary.streams:
        if row.kind == "raw" and row.received == 0:
            summary.warnings.append(f"{row.stream_id}: no samples received")
            continue
        if row.device_dropped:
            summary.warnings.append(
                f"{row.stream_id}: {row.device_dropped} samples dropped on the device"
                + (" (counter shared with another stream)" if row.device_shared else "")
            )
        if row.transport_dropped:
            summary.warnings.append(
                f"{row.stream_id}: {row.transport_dropped} samples lost in transport"
            )
        if row.processor_dropped:
            summary.warnings.append(
                f"{row.stream_id}: {row.processor_dropped} samples dropped at processor queues"
            )
        if (
            row.kind == "raw"
            and row.nominal_rate_hz
            and row.effective_rate_hz is not None
            and row.received > 10
            and row.effective_rate_hz < 0.8 * row.nominal_rate_hz
        ):
            summary.warnings.append(
                f"{row.stream_id}: effective {row.effective_rate_hz:.1f} Hz is below"
                f" 80 % of nominal {row.nominal_rate_hz:g} Hz"
            )
    for name, q in summary.app_queues.items():
        if q["dropped"]:
            summary.warnings.append(f"app queue {name!r} dropped {q['dropped']} items")
    for pid, p in summary.processors.items():
        if p.get("errors"):
            summary.warnings.append(f"processor {pid!r} raised {p['errors']} error(s)")
    if disk_free_bytes is not None and disk_free_bytes < 2_000_000_000:
        summary.warnings.append(f"only {format_bytes(disk_free_bytes)} free on the session disk")
    for w in extra_warnings or []:
        if w not in summary.warnings:
            summary.warnings.append(w)
    return summary


def _getter(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return lambda k: obj.get(k)
    return lambda k: getattr(obj, k, None)


def _add(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b
