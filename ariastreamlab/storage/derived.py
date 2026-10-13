"""Bus subscriber that persists derived streams (and raw streams when no VRS covers them).

One :class:`DerivedRecorder` owns one writer per stream and a single writer thread. It
subscribes with the ``BLOCK`` policy so a slow disk produces back-pressure on processors
(visible as queue depth) instead of silent loss. Raw streams are recorded here only for
sources that cannot write a VRS (the mock source); for the glasses the SDK's VRS *is* the raw
record (invariant I-2), and the recorder refuses to write under ``raw/``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ariastreamlab.config.models import StorageConfig
from ariastreamlab.core.bus import DropPolicy, EventBus, Subscription
from ariastreamlab.core.logging import get_logger
from ariastreamlab.core.registry import REGISTRY
from ariastreamlab.core.sample import AnySample, DerivedSample, Sample
from ariastreamlab.storage.layout import SessionLayout
from ariastreamlab.storage.schemas import payload_kind_of
from ariastreamlab.storage.writers import (
    ImageSequenceWriter,
    ParquetChunkWriter,
    StreamWriter,
    VideoWriter,
    write_meta,
)

log = get_logger("ariastreamlab.storage.derived")


@dataclass(frozen=True)
class RecorderStreamStats:
    """Per-stream recorder counters."""

    stream_id: str
    kind: str
    samples: int
    chunks: int
    bytes_written: int
    directory: str


class DerivedRecorder:
    """Persist samples from the bus into the session directory.

    Args:
        bus: The experiment's bus.
        layout: Session layout.
        storage: Storage options (image format, chunk length…).
        derived_streams: Derived stream ids whose processor has ``save_output`` set.
        raw_streams: Raw stream ids to persist as files (mock sessions only).
        nominal_rates: Stream id → nominal Hz, used for MP4 container timing.
    """

    def __init__(
        self,
        bus: EventBus,
        layout: SessionLayout,
        storage: StorageConfig,
        *,
        derived_streams: Iterable[str] = (),
        raw_streams: Iterable[str] = (),
        nominal_rates: dict[str, float | None] | None = None,
        queue_size: int = 512,
    ) -> None:
        self._bus = bus
        self._layout = layout
        self._storage = storage
        self._derived = set(derived_streams)
        self._raw = set(raw_streams)
        self._rates = dict(nominal_rates or {})
        self._writers: dict[str, StreamWriter] = {}
        self._kinds: dict[str, str] = {}
        self._subs: list[Subscription[Any]] = []
        self._queue_size = queue_size
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.dropped_unknown = 0
        self.errors: list[str] = []

    # -- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        """Subscribe and start the writer thread."""
        if self._derived:
            self._subs.append(
                self._bus.subscribe(
                    "derived.*",
                    maxsize=self._queue_size,
                    policy=DropPolicy.BLOCK,
                    name="recorder:derived",
                )
            )
        if self._raw:
            self._subs.append(
                self._bus.subscribe(
                    "raw.*", maxsize=self._queue_size, policy=DropPolicy.BLOCK, name="recorder:raw"
                )
            )
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="derived-recorder", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 30.0) -> None:
        """Drain the queues, flush and close every writer. Idempotent."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout)
            self._thread = None
        for sub in self._subs:
            sub.close()
        self._subs.clear()
        for sid, w in self._writers.items():
            try:
                w.close()
                write_meta(
                    w.directory,
                    {
                        **w.meta(),
                        "samples": w.stats.samples,
                        "chunks": w.stats.chunks,
                        "bytes": w.stats.bytes_written,
                        "units": dict(REGISTRY.get(sid).units) if sid in REGISTRY else {},
                    },
                )
            except Exception as exc:
                self.errors.append(f"{sid}: {type(exc).__name__}: {exc}")
                log.exception("recorder.close_failed", stream=sid)

    # -- stats -------------------------------------------------------------------------
    def stats(self) -> dict[str, RecorderStreamStats]:
        """Per-stream counters."""
        return {
            sid: RecorderStreamStats(
                sid,
                self._kinds.get(sid, "?"),
                w.stats.samples,
                w.stats.chunks,
                w.stats.bytes_written,
                str(w.directory),
            )
            for sid, w in self._writers.items()
        }

    def queue_depth(self) -> int:
        """Items waiting to be written."""
        return sum(len(s) for s in self._subs)

    # -- internals ---------------------------------------------------------------------
    def _run(self) -> None:
        last_check = time.monotonic()
        while True:
            got = False
            for sub in self._subs:
                item = sub.get(timeout=0.05)
                if item is not None:
                    got = True
                    self._handle(item)
            now = time.monotonic()
            if now - last_check >= 1.0:
                last_check = now
                for w in self._writers.values():
                    try:
                        w.maybe_flush()
                    except Exception as exc:
                        self.errors.append(f"{w.stream_id}: flush: {exc}")
                        log.exception("recorder.flush_failed", stream=w.stream_id)
            if self._stop.is_set() and not got and all(len(s) == 0 for s in self._subs):
                break

    def _handle(self, sample: AnySample) -> None:
        sid = sample.stream_id
        if isinstance(sample, DerivedSample):
            if sid not in self._derived:
                return
            base = self._layout.derived_stream(sid)
        else:
            if sid not in self._raw:
                return
            base = self._layout.raw_mock_stream(sid)
        writer = self._writers.get(sid)
        if writer is None:
            try:
                writer = self._make_writer(sid, sample, base)
            except Exception as exc:
                self.dropped_unknown += 1
                self.errors.append(f"{sid}: {type(exc).__name__}: {exc}")
                log.exception("recorder.writer_failed", stream=sid)
                self._derived.discard(sid)
                self._raw.discard(sid)
                return
            self._writers[sid] = writer
        try:
            writer.write(sample)
        except Exception as exc:
            self.errors.append(f"{sid}: {type(exc).__name__}: {exc}")
            log.exception("recorder.write_failed", stream=sid)

    def _make_writer(self, sid: str, sample: AnySample, directory: Path) -> StreamWriter:
        self._layout.guard_raw_write(directory)
        kind = (
            REGISTRY.get(sid).payload
            if (isinstance(sample, Sample) and sid in REGISTRY)
            else payload_kind_of(sample.data)
        )
        self._kinds[sid] = kind
        chunk = self._storage.chunk_seconds
        if kind == "image":
            fmt = self._storage.image_format
            if fmt == "mp4":
                fps = self._rates.get(sid) or 10.0
                return VideoWriter(directory, sid, fps=float(fps), chunk_seconds=chunk)
            return ImageSequenceWriter(
                directory,
                sid,
                fmt=fmt,
                jpeg_quality=self._storage.jpeg_quality,
                chunk_seconds=chunk,
            )
        return ParquetChunkWriter(directory, sid, kind, chunk_seconds=chunk)
