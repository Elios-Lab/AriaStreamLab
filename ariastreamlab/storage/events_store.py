"""Persist the event log (markers, device-state changes, config changes…) as Parquet."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ariastreamlab.events.markers import Event
from ariastreamlab.storage.writers import write_table_atomic

EVENTS_SCHEMA = pa.schema(
    [
        pa.field("device_timestamp_ns", pa.int64()),
        pa.field("host_timestamp_ns", pa.int64()),
        pa.field("kind", pa.string()),
        pa.field("name", pa.string()),
        pa.field("timestamp_source", pa.string()),
        pa.field("payload", pa.string()),
    ]
)


def write_events(path: Path, events: Iterable[Event]) -> int:
    """Write all events (sorted by device time) to ``path``; returns the row count."""
    rows = sorted(events, key=lambda e: (e.device_timestamp_ns, e.host_timestamp_ns))
    table = pa.table(
        {
            "device_timestamp_ns": [e.device_timestamp_ns for e in rows],
            "host_timestamp_ns": [e.host_timestamp_ns for e in rows],
            "kind": [e.kind for e in rows],
            "name": [e.name for e in rows],
            "timestamp_source": [e.timestamp_source for e in rows],
            "payload": [e.payload_json() for e in rows],
        },
        schema=EVENTS_SCHEMA,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_table_atomic(path, table)
    return len(rows)


def read_events(path: Path) -> list[Event]:
    """Read events back."""
    import json

    table = pq.read_table(path)
    out: list[Event] = []
    for r in table.to_pylist():
        out.append(
            Event(
                r["kind"],
                r["name"],
                int(r["device_timestamp_ns"]),
                int(r["host_timestamp_ns"]),
                json.loads(r["payload"]) if r["payload"] else {},
                r["timestamp_source"],
            )
        )
    return out
