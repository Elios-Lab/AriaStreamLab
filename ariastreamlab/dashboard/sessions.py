"""The sessions browser behind the app's *Sessions* page (no NiceGUI needed).

Lists the session directories under a root (the experiment's ``storage.root``), reads each
manifest without loading any data, and gives the page what it shows: one row per session
and, on request, the quality summary text and the events of one of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ariastreamlab.monitoring.quality import QualitySummary, format_bytes

__all__ = ["SessionRow", "list_sessions", "session_details"]


@dataclass
class SessionRow:
    """One session directory as the table shows it."""

    path: Path
    name: str
    experiment: str = ""
    participant: str = ""
    origin: str = ""
    status: str = ""
    started_utc: str = ""
    duration_s: float | None = None
    streams: int = 0
    events: int = 0
    warnings: int = 0
    problem: str | None = None
    """Set when the manifest could not be read (the row is still listed)."""

    def as_row(self) -> dict[str, Any]:
        """The dict a ``ui.table`` row takes."""
        return {
            "name": self.name,
            "experiment": self.experiment,
            "participant": self.participant,
            "origin": self.origin,
            "status": self.status if self.problem is None else f"unreadable: {self.problem}",
            "started": self.started_utc[:19].replace("T", " "),
            "duration": "" if self.duration_s is None else f"{self.duration_s:.0f} s",
            "streams": self.streams,
            "events": self.events,
            "warnings": self.warnings,
            "path": str(self.path),
        }


def list_sessions(root: Path | str) -> list[SessionRow]:
    """Every directory under ``root`` holding a ``manifest.json``, newest first."""
    rootp = Path(root)
    if not rootp.is_dir():
        return []
    rows: list[SessionRow] = []
    for d in sorted(rootp.iterdir(), reverse=True):
        manifest = d / "manifest.json"
        if not d.is_dir() or not manifest.exists():
            continue
        row = SessionRow(path=d, name=d.name)
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
            row.experiment = str(m.get("experiment", ""))
            row.participant = str(m.get("participant", ""))
            row.origin = str(m.get("origin", ""))
            row.status = str(m.get("status", ""))
            row.started_utc = str(m.get("started_utc", ""))
            row.duration_s = m.get("duration_s")
            row.streams = len(m.get("streams") or {})
            row.events = int(m.get("events") or 0)
            row.warnings = len(m.get("warnings") or [])
        except Exception as exc:
            row.problem = str(exc)[:80]
        rows.append(row)
    return rows


@dataclass
class SessionDetails:
    """What the page shows for the selected session."""

    path: Path
    summary_text: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    intervals: list[dict[str, Any]] = field(default_factory=list)
    raw_files: list[str] = field(default_factory=list)
    size_bytes: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def size_text(self) -> str:
        return format_bytes(self.size_bytes)


def session_details(path: Path | str) -> SessionDetails:
    """Manifest, quality summary, events and intervals of one session (read-only)."""
    from ariastreamlab.monitoring.health import directory_size
    from ariastreamlab.storage.reader import SessionReader

    p = Path(path)
    out = SessionDetails(path=p)
    try:
        out.manifest = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
    except Exception as exc:
        out.problems.append(f"manifest: {exc}")
        return out
    quality = out.manifest.get("quality") or {}
    if quality:
        try:
            out.summary_text = QualitySummary.from_dict(quality).text()
        except Exception as exc:
            out.problems.append(f"quality summary: {exc}")
    elif out.manifest.get("status") == "running":
        out.problems.append(
            "no quality summary: the session did not finalise (crash or still running)"
        )
    out.raw_files = list((out.manifest.get("raw") or {}).get("files") or [])
    out.size_bytes = directory_size(p)
    try:
        reader = SessionReader(p)
        t0 = None
        rng = reader.time_range()
        if rng is not None:
            t0 = rng[0]
        evs = reader.events()
        if t0 is None and evs:
            t0 = min(e.device_timestamp_ns for e in evs)
        base = t0 or 0
        out.events = [
            {
                "t_s": round((e.device_timestamp_ns - base) / 1e9, 3),
                "kind": e.kind,
                "name": e.name,
                "source": e.timestamp_source,
                "payload": e.payload_json()[:120],
            }
            for e in evs
        ]
        out.intervals = [
            {
                "name": i.name,
                "start_s": round((i.start_ns - base) / 1e9, 3),
                "end_s": None if i.end_ns is None else round((i.end_ns - base) / 1e9, 3),
                "duration_s": None if i.duration_s is None else round(i.duration_s, 3),
                "payload": json.dumps(dict(i.payload), default=str)[:120],
            }
            for i in reader.intervals()
        ]
    except Exception as exc:
        out.problems.append(f"events: {exc}")
    return out
