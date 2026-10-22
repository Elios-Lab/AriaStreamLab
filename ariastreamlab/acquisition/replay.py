"""Replay sources: feed recorded data into the same bus the live source uses.

:class:`PacedReplay` is the common engine (one pacing thread, speed/pause/seek). Backends
only provide an ordered iterator of samples from a start timestamp:

* :class:`SessionReplaySource` — a session recorded by AriaStreamLab without a VRS (mock
  sessions); pure Python, no SDK needed.
* ``VrsReplaySource`` / ``SdkReplaySource`` — Aria VRS recordings, in
  :mod:`ariastreamlab.acquisition.aria_gen2.vrs_replay` (they need ``projectaria_tools`` /
  the client SDK and are imported lazily by :func:`make_replay_source`).

Replayed samples keep their device timestamps and sequence numbers; only
``host_receive_timestamp_ns`` (the replay machine's clock) and ``provenance`` differ from the
original live samples — invariant I-1.
"""

from __future__ import annotations

import threading
from abc import abstractmethod
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ariastreamlab.acquisition.base import Source, SourceCapabilities, SourceStats
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import ReplayError
from ariastreamlab.core.logging import get_logger
from ariastreamlab.core.sample import Provenance, Sample
from ariastreamlab.timing.clock import HostClock

log = get_logger("ariastreamlab.acquisition.replay")


class PacedReplay(Source):
    """Base class: paces an ordered sample iterator against the host clock.

    Args:
        streams: Stream ids this source will publish.
        source_id: Provenance source id (``"vrs:<path>"``, ``"session:<name>"``).
        session_id: The *new* session id the replayed samples belong to.
        speed: 1.0 realtime, 2.0 twice as fast, 0.0 as fast as possible.
        start_ns / end_ns: Optional device-time window.
        clock: Host clock (fake in tests).
    """

    def __init__(
        self,
        *,
        streams: tuple[str, ...],
        source_id: str,
        session_id: str,
        speed: float = 1.0,
        start_ns: int | None = None,
        end_ns: int | None = None,
        clock: HostClock | None = None,
        device_id: str = "unknown",
    ) -> None:
        if speed < 0:
            raise ReplayError("replay speed must be >= 0 (0 = as fast as possible)")
        self.capabilities = SourceCapabilities(
            origin="replay",
            streams=streams,
            can_record_raw=False,
            is_paced=speed > 0,
            supports_replay_control=True,
        )
        self._provenance = Provenance("replay", source_id, session_id)
        self.device_id = device_id
        self._speed = speed
        self._start_ns = start_ns
        self._end_ns = end_ns
        self._clock = clock or HostClock()
        self._bus: EventBus | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._seek_to: int | None = None
        self._lock = threading.Lock()
        self._received: Counter[str] = Counter()
        self._position_ns: int | None = None
        self._first_ns: int | None = None

    # -- backend ------------------------------------------------------------------------
    @abstractmethod
    def _iter_from(self, start_ns: int | None) -> Iterator[Sample]:
        """Yield samples in device-time order starting at ``start_ns`` (inclusive)."""

    def time_range(self) -> tuple[int, int] | None:
        """``(first, last)`` device timestamp of the recording, when known."""
        return None

    # -- Source --------------------------------------------------------------------------
    def start(self, bus: EventBus) -> None:  # noqa: D102
        if self._thread is not None:
            raise ReplayError("replay already started")
        self._bus = bus
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="replay-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:  # noqa: D102
        self._stop.set()
        self._resume.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=10)
        self._thread = None
        self.finished.set()

    def stats(self) -> SourceStats:  # noqa: D102
        return SourceStats(
            dict(self._received),
            {},
            {"speed": self._speed, "position_ns": self._position_ns, "paused": self.paused},
        )

    # -- ReplayControl -----------------------------------------------------------------
    def pause(self) -> None:  # noqa: D102
        self._resume.clear()

    def resume(self) -> None:  # noqa: D102
        self._resume.set()

    @property
    def paused(self) -> bool:  # noqa: D102
        return not self._resume.is_set()

    def seek(self, device_timestamp_ns: int) -> None:  # noqa: D102
        with self._lock:
            self._seek_to = int(device_timestamp_ns)
        self._resume.set()

    def set_speed(self, multiplier: float) -> None:  # noqa: D102
        if multiplier < 0:
            raise ReplayError("speed must be >= 0")
        with self._lock:
            self._speed = float(multiplier)  # the engine re-anchors pacing when it notices

    @property
    def position_ns(self) -> int | None:
        """Device timestamp of the last replayed sample."""
        return self._position_ns

    @property
    def speed(self) -> float:
        """Current speed multiplier."""
        return self._speed

    # -- engine ------------------------------------------------------------------------
    def _pending_seek(self) -> int | None:
        """Seek target set from another thread (read under the lock)."""
        with self._lock:
            return self._seek_to

    def _run(self) -> None:
        bus = self._bus
        assert bus is not None
        start = self._start_ns
        try:
            while not self._stop.is_set():
                with self._lock:
                    self._seek_to = None
                    speed = self._speed
                anchor_dev: int | None = None
                anchor_host = 0
                restarted = False
                for sample in self._iter_from(start):
                    if self._stop.is_set():
                        break
                    ts = sample.device_timestamp_ns
                    if self._end_ns is not None and ts > self._end_ns:
                        break
                    if not self._resume.is_set():  # paused
                        self._resume.wait()
                        anchor_dev = None  # re-anchor after a pause
                    seek_to = self._pending_seek()
                    if seek_to is not None:
                        start = seek_to
                        restarted = True
                        break
                    with self._lock:
                        if self._speed != speed:
                            speed = self._speed
                            anchor_dev = None
                    if speed > 0:
                        if anchor_dev is None:
                            anchor_dev, anchor_host = ts, self._clock.now_ns()
                        else:
                            self._clock.sleep_until_ns(anchor_host + int((ts - anchor_dev) / speed))
                    if self._stop.is_set():
                        break
                    sample = self._restamp(sample)
                    bus.publish(f"raw.{sample.stream_id}", sample)
                    self._received[sample.stream_id] += 1
                    self._position_ns = ts
                    if self._first_ns is None:
                        self._first_ns = ts
                if not restarted:
                    break
        except Exception:
            log.exception("replay.failed")
        finally:
            self.finished.set()

    def _restamp(self, sample: Sample) -> Sample:
        return Sample(
            sample.device_id,
            sample.stream_id,
            sample.device_timestamp_ns,
            self._clock.now_ns(),
            sample.sequence_number,
            sample.data,
            self._provenance,
            sample.metadata,
        )


class SessionReplaySource(PacedReplay):
    """Replay the raw streams stored by AriaStreamLab in a session without a VRS."""

    def __init__(
        self,
        session_root: Path | str,
        *,
        session_id: str,
        streams: list[str] | None = None,
        **kw: Any,
    ) -> None:
        from ariastreamlab.storage.reader import SessionReader

        self._reader = SessionReader(session_root)
        raw = [sid for sid, st in self._reader.streams.items() if st.kind == "raw"]
        if not raw:
            raise ReplayError(
                f"{session_root} holds no raw stream files to replay",
                hint="live sessions replay raw/*.vrs; mock sessions need save_mock_raw=true",
            )
        wanted = raw if streams is None else [s for s in streams if s in raw]
        # calibration is metadata every consumer may need, never part of a stream subset
        # (the same rule as the live source and the VRS replay)
        if "device_calibration" in raw and "device_calibration" not in wanted:
            wanted = [*wanted, "device_calibration"]
        self._streams = wanted
        super().__init__(
            streams=tuple(wanted),
            source_id=f"session:{Path(session_root).name}",
            session_id=session_id,
            device_id=self._reader.manifest.device.serial or "mock-0",
            **kw,
        )

    def _iter_from(self, start_ns: int | None) -> Iterator[Sample]:
        for s in self._reader.iter_all(self._streams, start_ns=start_ns):
            if isinstance(s, Sample):
                yield Sample(
                    self.device_id,
                    s.stream_id,
                    s.device_timestamp_ns,
                    s.host_receive_timestamp_ns,
                    s.sequence_number,
                    s.data,
                    s.provenance,
                    s.metadata,
                )

    def time_range(self) -> tuple[int, int] | None:  # noqa: D102
        return self._reader.time_range(self._streams)


def make_replay_source(
    path: Path | str,
    *,
    session_id: str,
    speed: float = 1.0,
    start_s: float | None = None,
    end_s: float | None = None,
    backend: str = "tools",
    streams: list[str] | None = None,
    clock: HostClock | None = None,
) -> Source:
    """Pick the right replay backend for ``path``.

    ``path`` may be a session directory (replays ``raw/*.vrs`` when present, otherwise the
    stored raw streams) or a ``.vrs`` file. ``start_s``/``end_s`` are seconds relative to the
    first sample of the recording.
    """
    p = Path(path)
    if p.is_dir() and (p / "manifest.json").exists():
        vrs = sorted((p / "raw").glob("*.vrs"))
        if vrs:
            return _vrs_source(
                vrs[0],
                session_id=session_id,
                speed=speed,
                start_s=start_s,
                end_s=end_s,
                backend=backend,
                streams=streams,
                clock=clock,
                from_session=p,
            )
        src = SessionReplaySource(
            p, session_id=session_id, streams=streams, speed=speed, clock=clock
        )
        _apply_window(src, start_s, end_s)
        return src
    if p.is_file() and p.suffix == ".vrs":
        return _vrs_source(
            p,
            session_id=session_id,
            speed=speed,
            start_s=start_s,
            end_s=end_s,
            backend=backend,
            streams=streams,
            clock=clock,
            from_session=None,
        )
    raise ReplayError(f"{p} is neither a session directory nor a .vrs file")


def _vrs_source(vrs: Path, **kw: Any) -> Source:
    try:
        from ariastreamlab.acquisition.aria_gen2 import vrs_replay
    except ImportError as exc:  # pragma: no cover - depends on the optional SDK
        raise ReplayError(
            "replaying a VRS file needs projectaria_tools (pip install 'ariastreamlab[aria]')",
            hint=str(exc),
        ) from exc
    backend = kw.pop("backend")
    start_s, end_s = kw.pop("start_s"), kw.pop("end_s")
    kw.pop("from_session", None)
    if backend == "sdk":
        return vrs_replay.SdkReplaySource(vrs, start_s=start_s, end_s=end_s, **kw)
    src = vrs_replay.VrsReplaySource(vrs, **kw)
    _apply_window(src, start_s, end_s)
    return src


def _apply_window(src: PacedReplay, start_s: float | None, end_s: float | None) -> None:
    if start_s is None and end_s is None:
        return
    rng = src.time_range()
    if rng is None:
        raise ReplayError("cannot apply a time window: the recording has no readable time range")
    first = rng[0]
    if start_s is not None:
        src._start_ns = first + int(start_s * 1e9)
    if end_s is not None:
        src._end_ns = first + int(end_s * 1e9)
