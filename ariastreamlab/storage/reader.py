"""Read a session directory back: manifest, stream metadata and samples.

:class:`SessionReader` reconstructs :class:`~ariastreamlab.core.sample.Sample` objects from
the chunk files (Parquet rows, image files or video segments), so a mock session can be
replayed through the very same bus and processors (invariant I-1), and so ``inspect`` and
analysis notebooks have one entry point.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from ariastreamlab.core.errors import StorageError
from ariastreamlab.core.sample import DerivedSample, Provenance, Sample
from ariastreamlab.storage.layout import SessionLayout
from ariastreamlab.storage.manifest import Manifest
from ariastreamlab.storage.schemas import row_to_payload
from ariastreamlab.storage.timeline import stream_directories


@dataclass(frozen=True)
class StoredStream:
    """A file-backed stream found in a session."""

    stream_id: str
    kind: str
    """``raw`` (from ``raw_mock/``) or ``derived``."""
    directory: Path
    meta: dict[str, Any]

    @property
    def payload(self) -> str:
        """Payload kind as recorded in ``_meta.json`` (``image`` for cameras)."""
        return str(self.meta.get("payload", "dict"))

    @property
    def fmt(self) -> str:
        """``parquet``, ``image_sequence`` or ``video``."""
        return str(self.meta.get("format", "parquet"))


class SessionReader:
    """Random/sequential access to a stored session."""

    def __init__(self, root: Path | str) -> None:
        self.layout = SessionLayout.open_existing(root)
        self.manifest = Manifest.load(self.layout.manifest)
        self._streams: dict[str, StoredStream] = {}
        for sid, kind, d in stream_directories(self.layout):
            meta_path = d / "_meta.json"
            meta: dict[str, Any] = {}
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            else:  # crashed before _meta.json: infer from the files present
                meta = _infer_meta(d)
            self._streams[sid] = StoredStream(sid, kind, d, meta)

    # -- discovery ---------------------------------------------------------------------
    @property
    def streams(self) -> dict[str, StoredStream]:
        """Stream id → stored stream."""
        return dict(self._streams)

    def raw_vrs_files(self) -> list[Path]:
        """VRS files under ``raw/`` (live sessions)."""
        return sorted(self.layout.raw.glob("*.vrs"))

    def provenance(self) -> Provenance:
        """Provenance for samples re-read from this session."""
        return Provenance("replay", f"session:{self.layout.root.name}", self.manifest.session_id)

    # -- samples -----------------------------------------------------------------------
    def iter_stream(
        self,
        stream_id: str,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        load_images: bool = True,
    ) -> Iterator[Sample | DerivedSample]:
        """Yield the stored samples of one stream in device-time order."""
        st = self._streams.get(stream_id)
        if st is None:
            raise StorageError(
                f"stream {stream_id!r} is not stored in {self.layout.root}",
                hint=f"stored streams: {', '.join(self._streams) or 'none'}",
            )
        if st.fmt == "parquet":
            yield from self._iter_parquet(st, start_ns, end_ns)
        else:
            yield from self._iter_images(st, start_ns, end_ns, load_images)

    def iter_all(
        self,
        stream_ids: list[str] | None = None,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        load_images: bool = True,
    ) -> Iterator[Sample | DerivedSample]:
        """Merge several streams by device timestamp (the replay order)."""
        ids = list(self._streams) if stream_ids is None else stream_ids
        iters = [
            self.iter_stream(s, start_ns=start_ns, end_ns=end_ns, load_images=load_images)
            for s in ids
        ]
        yield from heapq.merge(*iters, key=lambda s: (s.device_timestamp_ns, s.stream_id))

    def time_range(self, stream_ids: list[str] | None = None) -> tuple[int, int] | None:
        """``(first, last)`` device timestamp across the streams, from the index files only."""
        lo: int | None = None
        hi: int | None = None
        for sid in stream_ids or list(self._streams):
            st = self._streams[sid]
            for part in sorted(st.directory.glob("part-*.parquet")) + sorted(
                st.directory.glob("frames-*.parquet")
            ):
                try:
                    col = pq.read_table(part, columns=["device_timestamp_ns"]).column(0)
                except Exception:
                    continue
                if col.length() == 0:
                    continue
                a, b = int(np.min(col.to_numpy())), int(np.max(col.to_numpy()))
                lo = a if lo is None else min(lo, a)
                hi = b if hi is None else max(hi, b)
        return None if lo is None or hi is None else (lo, hi)

    # -- internals ---------------------------------------------------------------------
    def _make(self, st: StoredStream, row: dict[str, Any], data: Any) -> Sample | DerivedSample:
        prov = self.provenance()
        device_id = self.manifest.device.serial or (
            "mock-0" if self.manifest.origin == "mock" else "unknown"
        )
        if st.kind == "raw":
            seq = int(row["sequence_number"])
            return Sample(
                device_id,
                st.stream_id,
                int(row["device_timestamp_ns"]),
                int(row["host_timestamp_ns"]),
                None if seq < 0 else seq,
                data,
                prov,
                {"label": st.meta.get("label", st.stream_id)},
            )
        producer = st.meta.get("producer") or {}
        proc = str(producer.get("processor") or st.meta.get("processor_id", "unknown"))
        return DerivedSample(
            device_id,
            st.stream_id,
            int(row["device_timestamp_ns"]),
            tuple(producer.get("inputs") or st.meta.get("source_streams", ())),
            (),
            proc,
            int(row["host_timestamp_ns"]),
            int(row["host_timestamp_ns"]),
            data,
            prov,
        )

    def _iter_parquet(
        self, st: StoredStream, start_ns: int | None, end_ns: int | None
    ) -> Iterator[Sample | DerivedSample]:
        for part in sorted(st.directory.glob("part-*.parquet")):
            try:
                table = pq.read_table(part)
            except Exception:
                continue
            for row in table.to_pylist():
                ts = int(row["device_timestamp_ns"])
                if (start_ns is not None and ts < start_ns) or (end_ns is not None and ts > end_ns):
                    continue
                yield self._make(st, row, row_to_payload(row, st.payload))

    def _iter_images(
        self, st: StoredStream, start_ns: int | None, end_ns: int | None, load: bool
    ) -> Iterator[Sample | DerivedSample]:
        import cv2

        video_cache: dict[str, Any] = {}
        for part in sorted(st.directory.glob("frames-*.parquet")):
            try:
                table = pq.read_table(part)
            except Exception:
                continue
            for row in table.to_pylist():
                ts = int(row["device_timestamp_ns"])
                if (start_ns is not None and ts < start_ns) or (end_ns is not None and ts > end_ns):
                    continue
                data: Any = None
                if load:
                    path = st.directory / row["file"]
                    if st.fmt == "video":
                        cap = video_cache.get(row["file"])
                        if cap is None:
                            cap = video_cache[row["file"]] = cv2.VideoCapture(str(path))
                        cap.set(cv2.CAP_PROP_POS_FRAMES, int(row["frame_index"]))
                        ok, frame = cap.read()
                        if not ok:
                            continue
                        data = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if frame.ndim == 3 else frame
                    else:
                        frame = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
                        if frame is None:
                            continue
                        data = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if frame.ndim == 3 else frame
                yield self._make(st, row, data)
        for cap in video_cache.values():
            cap.release()


def _infer_meta(directory: Path) -> dict[str, Any]:
    if any(directory.glob("video-*.mp4")):
        return {"format": "video", "payload": "image", "recovered": True}
    if any(directory.glob("frame-*.png")) or any(directory.glob("frame-*.jpg")):
        return {
            "format": "image_sequence",
            "payload": "image",
            "image_format": "png" if any(directory.glob("frame-*.png")) else "jpeg",
            "recovered": True,
        }
    parts = sorted(directory.glob("part-*.parquet"))
    kind = "dict"
    if parts:
        try:
            names = set(pq.read_schema(parts[0]).names)
        except Exception:
            names = set()
        from ariastreamlab.storage.schemas import SCHEMAS

        for k, schema in SCHEMAS.items():
            if set(schema.names) == names:
                kind = k
                break
    return {"format": "parquet", "payload": kind, "recovered": True}
