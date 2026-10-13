"""``indexes/timeline.parquet``: one row per stored sample across every file-backed stream.

Columns: ``stream_id``, ``kind`` (raw|derived), ``device_timestamp_ns``, ``host_timestamp_ns``,
``sequence_number``, ``file`` (relative to the session root), ``row`` (row inside that file, or
frame index inside a video segment). It is rebuilt from the chunk files alone, which is what
makes a crashed session inspectable.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ariastreamlab.storage.layout import SessionLayout
from ariastreamlab.storage.writers import write_table_atomic

TIMELINE_SCHEMA = pa.schema(
    [
        pa.field("stream_id", pa.string()),
        pa.field("kind", pa.string()),
        pa.field("device_timestamp_ns", pa.int64()),
        pa.field("host_timestamp_ns", pa.int64()),
        pa.field("sequence_number", pa.int64()),
        pa.field("file", pa.string()),
        pa.field("row", pa.int64()),
    ]
)


def stream_directories(layout: SessionLayout) -> list[tuple[str, str, Path]]:
    """``(stream_id, kind, directory)`` for every file-backed stream of the session."""
    out: list[tuple[str, str, Path]] = []
    for kind, base in (("raw", layout.raw_mock), ("derived", layout.derived)):
        if not base.exists():
            continue
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            out.append((d.name, kind, d))
    return out


def _index_rows_for_dir(root: Path, stream_id: str, kind: str, directory: Path) -> list[pa.Table]:
    tables: list[pa.Table] = []
    parts = sorted(directory.glob("part-*.parquet")) + sorted(directory.glob("frames-*.parquet"))
    for part in parts:
        try:
            t = pq.read_table(
                part,
                columns=["device_timestamp_ns", "host_timestamp_ns", "sequence_number"]
                + (["file", "frame_index"] if part.name.startswith("frames-") else []),
            )
        except Exception:
            continue
        n = t.num_rows
        if part.name.startswith("frames-"):
            files = [
                str(Path(kind if kind == "derived" else "raw_mock") / stream_id / f)
                for f in t.column("file").to_pylist()
            ]
            rows = t.column("frame_index").to_pylist()
        else:
            rel = str(part.relative_to(root))
            files = [rel] * n
            rows = list(range(n))
        tables.append(
            pa.table(
                {
                    "stream_id": [stream_id] * n,
                    "kind": [kind] * n,
                    "device_timestamp_ns": t.column("device_timestamp_ns"),
                    "host_timestamp_ns": t.column("host_timestamp_ns"),
                    "sequence_number": t.column("sequence_number"),
                    "file": files,
                    "row": rows,
                },
                schema=TIMELINE_SCHEMA,
            )
        )
    return tables


def build_timeline(layout: SessionLayout) -> pa.Table:
    """Scan every stream directory and return the sorted timeline table."""
    tables: list[pa.Table] = []
    for sid, kind, d in stream_directories(layout):
        tables.extend(_index_rows_for_dir(layout.root, sid, kind, d))
    if not tables:
        return TIMELINE_SCHEMA.empty_table()
    table = pa.concat_tables(tables)
    return table.sort_by([("device_timestamp_ns", "ascending"), ("stream_id", "ascending")])


def write_timeline(layout: SessionLayout) -> int:
    """Build and write the timeline; returns the row count."""
    table = build_timeline(layout)
    layout.indexes.mkdir(parents=True, exist_ok=True)
    write_table_atomic(layout.timeline, table)
    return int(table.num_rows)


def read_timeline(layout: SessionLayout) -> pa.Table:
    """Read ``timeline.parquet`` (building it if missing)."""
    if not layout.timeline.exists():
        write_timeline(layout)
    return pq.read_table(layout.timeline)
