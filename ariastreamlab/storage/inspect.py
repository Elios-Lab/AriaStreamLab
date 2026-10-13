"""``ariastreamlab inspect``: summarise a session directory and repair a crashed one."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ariastreamlab.core.errors import StorageError
from ariastreamlab.storage.layout import SessionLayout
from ariastreamlab.storage.manifest import Manifest
from ariastreamlab.storage.reader import SessionReader
from ariastreamlab.storage.timeline import write_timeline


@dataclass
class StreamSummary:
    """One stored stream."""

    stream_id: str
    kind: str
    fmt: str
    payload: str
    samples: int
    files: int
    bytes: int
    first_device_ns: int | None
    last_device_ns: int | None
    recovered: bool = False

    @property
    def duration_s(self) -> float | None:
        """Span of stored device timestamps."""
        if self.first_device_ns is None or self.last_device_ns is None:
            return None
        return (self.last_device_ns - self.first_device_ns) / 1e9


@dataclass
class SessionSummary:
    """What ``inspect`` prints."""

    root: Path
    manifest: Manifest
    streams: list[StreamSummary] = field(default_factory=list)
    raw_files: list[tuple[str, int]] = field(default_factory=list)
    events: int = 0
    timeline_rows: int | None = None
    repaired: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        """Disk usage of raw + stored streams."""
        return sum(s.bytes for s in self.streams) + sum(b for _, b in self.raw_files)


def _dir_bytes(d: Path) -> int:
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())


def inspect_session(root: Path | str, *, repair: bool = False) -> SessionSummary:
    """Summarise a session; with ``repair`` rebuild the timeline and mark a crash."""
    layout = SessionLayout.open_existing(root)
    try:
        reader = SessionReader(layout.root)
    except Exception as exc:
        raise StorageError(f"cannot read {layout.root}: {exc}") from exc
    summary = SessionSummary(layout.root, reader.manifest)
    if reader.manifest.status == "running":
        summary.problems.append(
            "manifest says 'running': the session did not finalise (crash or still in progress)"
        )
        if repair:
            reader.manifest.status = "crashed"
            reader.manifest.warnings.append("marked crashed by `inspect --repair`")
            reader.manifest.write(layout.manifest)
            summary.repaired = True
    for sid, st in reader.streams.items():
        parts = sorted(st.directory.glob("part-*.parquet")) + sorted(
            st.directory.glob("frames-*.parquet")
        )
        rng = reader.time_range([sid])
        samples = 0
        import pyarrow.parquet as pq

        for p in parts:
            try:
                samples += pq.read_metadata(p).num_rows
            except Exception:
                summary.problems.append(f"{sid}: unreadable chunk {p.name} (truncated by a crash?)")
        summary.streams.append(
            StreamSummary(
                sid,
                st.kind,
                st.fmt,
                st.payload,
                samples,
                len(parts),
                _dir_bytes(st.directory),
                None if rng is None else rng[0],
                None if rng is None else rng[1],
                bool(st.meta.get("recovered")),
            )
        )
        if st.meta.get("recovered"):
            summary.problems.append(f"{sid}: _meta.json missing, format inferred from files")
    for f in reader.raw_vrs_files():
        summary.raw_files.append((str(f.relative_to(layout.root)), f.stat().st_size))
    if layout.events_file.exists():
        import pyarrow.parquet as pq

        summary.events = pq.read_metadata(layout.events_file).num_rows
    if repair or not layout.timeline.exists():
        try:
            summary.timeline_rows = write_timeline(layout)
            if repair:
                summary.repaired = True
        except Exception as exc:
            summary.problems.append(f"timeline: {exc}")
    else:
        import pyarrow.parquet as pq

        summary.timeline_rows = pq.read_metadata(layout.timeline).num_rows
    for name in ("experiment.json",):
        if not (layout.root / name).exists():
            summary.problems.append(f"missing {name}")
    return summary


def summary_as_dict(s: SessionSummary) -> dict[str, Any]:
    """JSON-friendly form."""
    return {
        "root": str(s.root),
        "session_id": s.manifest.session_id,
        "status": s.manifest.status,
        "origin": s.manifest.origin,
        "experiment": s.manifest.experiment,
        "participant": s.manifest.participant,
        "started_utc": s.manifest.started_utc,
        "duration_s": s.manifest.duration_s,
        "raw": s.manifest.raw.model_dump(),
        "raw_files": [{"file": f, "bytes": b} for f, b in s.raw_files],
        "streams": [
            {
                "stream_id": x.stream_id,
                "kind": x.kind,
                "format": x.fmt,
                "payload": x.payload,
                "samples": x.samples,
                "files": x.files,
                "bytes": x.bytes,
                "duration_s": x.duration_s,
            }
            for x in s.streams
        ],
        "events": s.events,
        "timeline_rows": s.timeline_rows,
        "total_bytes": s.total_bytes,
        "problems": s.problems,
        "repaired": s.repaired,
        "free_disk_bytes": _free_bytes(s.root),
    }


def _free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize
