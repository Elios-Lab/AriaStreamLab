"""Chunked, crash-tolerant writers for derived (and mock-raw) streams.

Design (docs/ARCHITECTURE.md §6):

* **Tabular streams** → one Parquet file per chunk, ``part-00000.parquet`` …, each written to a
  temporary name and renamed atomically. A crash loses at most the rows buffered since the
  last chunk (``chunk_seconds`` of data, default 20 s), never an already-written file.
* **Image streams** → either an image sequence (``png``/``jpeg`` files, lossless or not) or an
  MP4 (``mp4v``, lossy), plus ``frames-NNNNN.parquet`` index chunks that carry the timestamps.
  MP4 files are rotated with the index so a crash leaves finished, playable segments.

All writers are single-consumer: :class:`~ariastreamlab.storage.derived.DerivedRecorder`
calls them from one thread.
"""

from __future__ import annotations

import json
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ariastreamlab.core.errors import StorageError
from ariastreamlab.core.sample import AnySample, Sample
from ariastreamlab.storage.schemas import SCHEMAS, TIME_COLUMNS, rows_to_table, sample_to_row

FRAME_INDEX_SCHEMA = pa.schema(
    [*TIME_COLUMNS, pa.field("frame_index", pa.int64()), pa.field("file", pa.string())]
)


@dataclass
class WriterStats:
    """Counters of one writer."""

    samples: int = 0
    chunks: int = 0
    bytes_written: int = 0
    last_flush_monotonic: float = field(default_factory=time.monotonic)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temporary file + rename (POSIX-atomic)."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    """Text variant of :func:`atomic_write_bytes`."""
    atomic_write_bytes(path, text.encode("utf-8"))


def write_table_atomic(path: Path, table: pa.Table) -> int:
    """Write a Parquet file atomically; returns the number of bytes written."""
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)
    return path.stat().st_size


class StreamWriter(ABC):
    """One output stream on disk."""

    def __init__(
        self, directory: Path, stream_id: str, *, chunk_seconds: float, max_rows: int = 200_000
    ) -> None:
        self.directory = directory
        self.stream_id = stream_id
        self.chunk_seconds = chunk_seconds
        self.max_rows = max_rows
        self.stats = WriterStats()
        self._lock = threading.Lock()
        self._closed = False
        directory.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def write(self, sample: AnySample) -> None:
        """Buffer one sample."""

    @abstractmethod
    def flush(self) -> None:
        """Persist everything buffered so far as a finished chunk."""

    def maybe_flush(self) -> None:
        """Flush when the chunk is old enough (called periodically by the recorder)."""
        if time.monotonic() - self.stats.last_flush_monotonic >= self.chunk_seconds:
            self.flush()

    def close(self) -> None:
        """Flush and finish. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.flush()

    @property
    def closed(self) -> bool:
        """True after :meth:`close`."""
        return self._closed

    def meta(self) -> dict[str, Any]:
        """Content of the stream's ``_meta.json``."""
        return {
            "stream_id": self.stream_id,
            "writer": type(self).__name__,
            "chunk_seconds": self.chunk_seconds,
        }


class ParquetChunkWriter(StreamWriter):
    """Tabular payloads → ``part-NNNNN.parquet`` chunks."""

    def __init__(self, directory: Path, stream_id: str, kind: str, **kw: Any) -> None:
        super().__init__(directory, stream_id, **kw)
        if kind not in SCHEMAS:
            raise StorageError(f"no tabular schema for payload kind {kind!r} (stream {stream_id})")
        self.kind = kind
        self._rows: list[dict[str, Any]] = []
        self._part = _next_part_index(directory, "part-")

    def write(self, sample: AnySample) -> None:  # noqa: D102
        self._rows.append(sample_to_row(sample, self.kind))
        self.stats.samples += 1
        if len(self._rows) >= self.max_rows:
            self.flush()

    def flush(self) -> None:  # noqa: D102
        with self._lock:
            rows, self._rows = self._rows, []
            if not rows:
                self.stats.last_flush_monotonic = time.monotonic()
                return
            path = self.directory / f"part-{self._part:05d}.parquet"
            rows.sort(key=lambda r: r["device_timestamp_ns"])  # late arrivals land in order
            self.stats.bytes_written += write_table_atomic(path, rows_to_table(rows, self.kind))
            self._part += 1
            self.stats.chunks += 1
            self.stats.last_flush_monotonic = time.monotonic()

    def meta(self) -> dict[str, Any]:  # noqa: D102
        return {
            **super().meta(),
            "format": "parquet",
            "payload": self.kind,
            "columns": SCHEMAS[self.kind].names,
        }


class ImageSequenceWriter(StreamWriter):
    """Image payloads → ``frame-<n>.png|jpg`` + ``frames-NNNNN.parquet`` index chunks."""

    def __init__(
        self,
        directory: Path,
        stream_id: str,
        *,
        fmt: str = "png",
        jpeg_quality: int = 90,
        **kw: Any,
    ) -> None:
        super().__init__(directory, stream_id, **kw)
        if fmt not in ("png", "jpeg"):
            raise StorageError(f"image format must be png or jpeg, got {fmt!r}")
        self.fmt = fmt
        self.jpeg_quality = jpeg_quality
        self._index_rows: list[dict[str, Any]] = []
        self._part = _next_part_index(directory, "frames-")
        self._frame = _count_frames(directory)

    def write(self, sample: AnySample) -> None:  # noqa: D102
        import cv2

        img = np.asarray(sample.data)
        name = f"frame-{self._frame:07d}.{'png' if self.fmt == 'png' else 'jpg'}"
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if img.ndim == 3 and img.shape[2] == 3 else img
        params = (
            [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
            if self.fmt == "jpeg"
            else [cv2.IMWRITE_PNG_COMPRESSION, 1]
        )
        ok, buf = cv2.imencode("." + ("png" if self.fmt == "png" else "jpg"), bgr, params)
        if not ok:
            raise StorageError(f"could not encode frame for {self.stream_id}")
        data = buf.tobytes()
        atomic_write_bytes(self.directory / name, data)
        self.stats.bytes_written += len(data)
        row = _frame_row(sample, self._frame, name)
        self._index_rows.append(row)
        self._frame += 1
        self.stats.samples += 1
        if len(self._index_rows) >= self.max_rows:
            self.flush()

    def flush(self) -> None:  # noqa: D102
        with self._lock:
            rows, self._index_rows = self._index_rows, []
            if not rows:
                self.stats.last_flush_monotonic = time.monotonic()
                return
            path = self.directory / f"frames-{self._part:05d}.parquet"
            rows.sort(key=lambda r: r["device_timestamp_ns"])
            self.stats.bytes_written += write_table_atomic(
                path,
                pa.table(
                    {f.name: [r[f.name] for r in rows] for f in FRAME_INDEX_SCHEMA},
                    schema=FRAME_INDEX_SCHEMA,
                ),
            )
            self._part += 1
            self.stats.chunks += 1
            self.stats.last_flush_monotonic = time.monotonic()

    def meta(self) -> dict[str, Any]:  # noqa: D102
        return {
            **super().meta(),
            "format": "image_sequence",
            "image_format": self.fmt,
            "payload": "image",
            "lossless": self.fmt == "png",
        }


class VideoWriter(StreamWriter):
    """Image payloads → ``video-NNNNN.mp4`` segments + ``frames-NNNNN.parquet`` index chunks.

    One MP4 segment per chunk, so a crash leaves playable segments. Frames are written at
    the stream's nominal rate for the container timing; the true timestamps live in the index.
    """

    def __init__(
        self, directory: Path, stream_id: str, *, fps: float = 10.0, codec: str = "mp4v", **kw: Any
    ) -> None:
        super().__init__(directory, stream_id, **kw)
        self.fps = fps
        self.codec = codec
        self._index_rows: list[dict[str, Any]] = []
        self._part = _next_part_index(directory, "frames-")
        self._frame = _count_frames(directory)
        self._segment_frame = 0
        self._writer: Any = None
        self._segment_path: Path | None = None
        self._size: tuple[int, int] | None = None
        self._is_color = True

    def _open_segment(self, img: np.ndarray) -> None:
        import cv2

        self._size = (int(img.shape[1]), int(img.shape[0]))
        self._is_color = img.ndim == 3
        self._segment_path = self.directory / f"video-{self._part:05d}.mp4"
        self._writer = cv2.VideoWriter(
            str(self._segment_path),
            cv2.VideoWriter.fourcc(*self.codec),
            self.fps,
            self._size,
            self._is_color,
        )
        if not self._writer.isOpened():
            raise StorageError(
                f"OpenCV could not open a {self.codec} video writer for {self.stream_id}",
                hint="set storage.image_format to 'png' or 'jpeg'",
            )
        self._segment_frame = 0

    def write(self, sample: AnySample) -> None:  # noqa: D102
        import cv2

        img = np.asarray(sample.data)
        if self._writer is None:
            self._open_segment(img)
        assert self._size is not None
        if (int(img.shape[1]), int(img.shape[0])) != self._size or (
            img.ndim == 3
        ) != self._is_color:
            self.flush()
            self._open_segment(img)
        frame = cv2.cvtColor(img, cv2.COLOR_RGB2BGR) if img.ndim == 3 and img.shape[2] == 3 else img
        self._writer.write(frame)
        assert self._segment_path is not None
        self._index_rows.append(
            {
                **_frame_row(sample, self._frame, self._segment_path.name),
                "frame_index": self._segment_frame,
            }
        )
        self._frame += 1
        self._segment_frame += 1
        self.stats.samples += 1
        if len(self._index_rows) >= self.max_rows:
            self.flush()

    def flush(self) -> None:  # noqa: D102
        with self._lock:
            rows, self._index_rows = self._index_rows, []
            if self._writer is not None:
                self._writer.release()
                self._writer = None
                if self._segment_path is not None and self._segment_path.exists():
                    self.stats.bytes_written += self._segment_path.stat().st_size
            if not rows:
                self.stats.last_flush_monotonic = time.monotonic()
                return
            path = self.directory / f"frames-{self._part:05d}.parquet"
            rows.sort(key=lambda r: r["device_timestamp_ns"])
            self.stats.bytes_written += write_table_atomic(
                path,
                pa.table(
                    {f.name: [r[f.name] for r in rows] for f in FRAME_INDEX_SCHEMA},
                    schema=FRAME_INDEX_SCHEMA,
                ),
            )
            self._part += 1
            self.stats.chunks += 1
            self.stats.last_flush_monotonic = time.monotonic()

    def meta(self) -> dict[str, Any]:  # noqa: D102
        return {
            **super().meta(),
            "format": "video",
            "codec": self.codec,
            "fps": self.fps,
            "payload": "image",
            "lossless": False,
        }


def _frame_row(sample: AnySample, frame: int, file: str) -> dict[str, Any]:
    if isinstance(sample, Sample):
        host = sample.host_receive_timestamp_ns
        seq = -1 if sample.sequence_number is None else int(sample.sequence_number)
    else:
        host = sample.processing_end_ns
        seq = -1
    return {
        "device_timestamp_ns": int(sample.device_timestamp_ns),
        "host_timestamp_ns": int(host),
        "sequence_number": seq,
        "frame_index": frame,
        "file": file,
    }


def _next_part_index(directory: Path, prefix: str) -> int:
    existing = sorted(directory.glob(f"{prefix}*.parquet"))
    if not existing:
        return 0
    return int(existing[-1].stem[len(prefix) :]) + 1


def _count_frames(directory: Path) -> int:
    return len(list(directory.glob("frame-*.*")))


def write_meta(directory: Path, meta: dict[str, Any]) -> None:
    """Write ``_meta.json`` for a stream directory."""
    atomic_write_text(
        directory / "_meta.json", json.dumps(meta, indent=2, sort_keys=True, default=str)
    )


__all__ = [
    "FRAME_INDEX_SCHEMA",
    "ImageSequenceWriter",
    "ParquetChunkWriter",
    "StreamWriter",
    "VideoWriter",
    "WriterStats",
    "atomic_write_bytes",
    "atomic_write_text",
    "write_meta",
    "write_table_atomic",
]
