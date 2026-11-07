"""Teardown robustness: what is left running, written or attached when something fails."""

from __future__ import annotations

import logging
import socket
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from ariastreamlab import Experiment
from ariastreamlab.acquisition.mock import MockSource
from ariastreamlab.config import load_dict
from ariastreamlab.config.models import DashboardConfig, StorageConfig
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import AriaStreamLabError
from ariastreamlab.core.sample import Provenance, Sample
from ariastreamlab.processing.base import Emit, Inputs, OutputSpec, Processor, ProcessorContext
from ariastreamlab.processing.graph import ProcessingGraph
from ariastreamlab.storage.derived import DerivedRecorder
from ariastreamlab.storage.layout import SessionLayout
from ariastreamlab.storage.manifest import Manifest


def _config(tmp_path: Path, **over: Any) -> Any:
    data: dict[str, Any] = {
        "experiment": {"name": "teardown", "participant": "P1"},
        "storage": {"root": str(tmp_path / "sessions")},
        "streams": {"imu": {}},
        "mock": {"seed": 1, "jitter_ms": 0.0, "realtime": False},
        "dashboard": {"enabled": False},
    }
    data.update(over)
    return load_dict(data)


# -- one source stop at a time ----------------------------------------------------------


def test_close_never_stops_the_source_while_the_run_thread_is_stopping_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = 0
    most = 0
    lock = threading.Lock()

    class SlowStop(MockSource):
        def stop(self) -> None:
            nonlocal active, most
            with lock:
                active += 1
                most = max(most, active)
            time.sleep(0.4)  # a live stop: reconnect, quiescence, VRS settle
            super().stop()
            with lock:
                active -= 1

    cfg = _config(tmp_path, mock={"realtime": True})
    exp = Experiment(cfg, source=SlowStop(cfg.enabled_streams(), mock=cfg.mock, session_id="s"))
    monkeypatch.setattr(Experiment, "RUN_JOIN_S", 0.05)  # close() gives up waiting at once
    monkeypatch.setattr(Experiment, "STOP_WAIT_S", 0.0)
    with exp:
        exp.start()
        time.sleep(0.3)
        exp.stop()
        time.sleep(0.05)  # the run thread is now inside source.stop()
    assert most == 1
    assert Manifest.load(exp.session_dir / "manifest.json").status == "completed"  # type: ignore[operator]


# -- the processing graph -----------------------------------------------------------------


class FailingSetup(Processor):
    version = "1"
    outputs = {"*": OutputSpec(payload="any")}

    def setup(self, ctx: ProcessorContext) -> None:
        raise RuntimeError("model file missing")

    def process(self, inputs: Inputs) -> Emit | None:
        return None


def test_a_failed_setup_stops_the_workers_already_started(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        processors=[
            {
                "id": "bad",
                "type": "tests.unit.test_teardown:FailingSetup",
                "inputs": ["imu_left"],
                "outputs": ["a"],
            },
            {"id": "copy", "type": "generic.passthrough", "inputs": ["a"], "outputs": ["b"]},
        ],
    )
    bus = EventBus()
    graph = ProcessingGraph(cfg, bus, health_interval_s=0)
    assert graph.order == ["bad", "copy"]  # "copy" starts first (downstream first)
    with pytest.raises(RuntimeError, match="model file missing"):
        graph.start()
    assert not graph.workers["copy"].alive
    assert bus.publish("raw.imu_left", object()) == 0  # nobody is subscribed any more
    bus.close()


# -- the recorder ---------------------------------------------------------------------------


class _SlowWriter:
    def __init__(self, directory: Path, stream_id: str) -> None:
        self.directory = directory
        self.stream_id = stream_id
        self.stats = types.SimpleNamespace(samples=0, chunks=0, bytes_written=0)
        self.closed = False
        self.after_close: list[str] = []
        self.in_write = threading.Event()

    def write(self, sample: Any) -> None:
        if self.closed:
            self.after_close.append("write")
        self.in_write.set()
        time.sleep(0.6)  # a slow disk
        if self.closed:
            self.after_close.append("write (closed underneath)")
        self.stats.samples += 1

    def maybe_flush(self) -> None:
        if self.closed:
            self.after_close.append("flush")

    def close(self) -> None:
        self.closed = True

    def meta(self) -> dict[str, Any]:
        return {"stream": self.stream_id}


def test_the_recorder_closes_its_writers_only_once_the_thread_has_let_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writers: list[_SlowWriter] = []

    def make(self: Any, sid: str, sample: Any, directory: Path) -> _SlowWriter:
        directory.mkdir(parents=True, exist_ok=True)
        w = _SlowWriter(directory, sid)
        writers.append(w)
        return w

    monkeypatch.setattr(DerivedRecorder, "_make_writer", make)
    bus = EventBus()
    layout = SessionLayout(tmp_path / "s")
    rec = DerivedRecorder(bus, layout, StorageConfig(), raw_streams=["imu_left"])
    rec.start()
    prov = Provenance("mock", "mock", "s")
    for i in range(3):
        bus.publish("raw.imu_left", Sample("mock-0", "imu_left", i, i, i, None, prov, {}))
    deadline = time.monotonic() + 2
    while not writers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert writers and writers[0].in_write.wait(2)
    rec.close(timeout=0.1)  # the thread is still writing the first sample
    assert writers[0].closed
    time.sleep(1.0)  # whatever the thread would still do
    assert writers[0].after_close == []
    assert any("did not drain" in e for e in rec.errors)
    bus.close()


# -- finalize -------------------------------------------------------------------------------


def _session_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.FileHandler) and h.baseFilename.endswith("session.log")
    ]


def test_a_failure_in_finalize_still_writes_the_manifest_and_detaches_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ariastreamlab.storage.session as session_mod

    def broken(*_: Any, **__: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(session_mod, "write_timeline", broken)
    before = len(_session_handlers())
    exp = Experiment(_config(tmp_path), mock=True, duration_s=0.5)
    with exp:
        exp.run()
    m = Manifest.load(exp.session_dir / "manifest.json")  # type: ignore[operator]
    assert m.status == "completed"
    assert any("timeline not written" in w and "disk full" in w for w in m.warnings)
    assert len(_session_handlers()) == before


def test_the_session_log_is_detached_even_when_the_manifest_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Any, path: Path) -> None:
        raise OSError("read-only file system")

    before = len(_session_handlers())
    exp = Experiment(_config(tmp_path), mock=True, duration_s=0.5)
    exp.open()
    exp.run()
    monkeypatch.setattr(Manifest, "write", broken)
    with pytest.raises(OSError, match="read-only"):
        exp.close()
    assert len(_session_handlers()) == before


# -- the dashboard on a busy port -------------------------------------------------------------


@pytest.fixture
def busy_port() -> Any:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen()
        yield s.getsockname()[1]


def test_a_busy_port_is_reported_before_the_server_starts(
    busy_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nicegui import ui

    from ariastreamlab.dashboard.app import Dashboard

    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(ui, "run", lambda **kw: runs.append(kw))
    dash = Dashboard(DashboardConfig(host="127.0.0.1", port=busy_port))
    # the application window: an error with a hint, not NiceGUI's sys.exit
    with pytest.raises(AriaStreamLabError, match=f"port {busy_port} is in use"):
        dash.serve(native=True)
    # a session's dashboard: start() says no, quickly
    t0 = time.monotonic()
    assert dash.start(timeout=5) is False
    assert time.monotonic() - t0 < 2
    assert runs == []  # NiceGUI was never started, so a later start can still succeed


# -- the SDK replay -----------------------------------------------------------------------------


def test_sdk_replay_stop_waits_for_the_callbacks_to_go_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ariastreamlab.acquisition.aria_gen2.vrs_replay import SdkReplaySource
    from ariastreamlab.timing.clock import HostClock

    clock = HostClock()
    binder = types.SimpleNamespace(last_host_ns=clock.now_ns(), _clock=clock)  # replaying
    still_firing = threading.Event()

    def sdk_threads() -> None:  # the SDK's dispatch threads, still in callbacks after stop()
        still_firing.wait(2)
        for _ in range(8):
            binder.last_host_ns = clock.now_ns()
            time.sleep(0.05)

    class Session:
        def stop(self) -> None:
            still_firing.set()

    released: list[int] = []
    handler = types.SimpleNamespace(clear_callbacks=lambda: released.append(clock.now_ns()))
    src = SdkReplaySource.__new__(SdkReplaySource)
    src._stop, src._watch = threading.Event(), None
    src._session, src._handler, src._binder = Session(), handler, binder
    monkeypatch.setattr(SdkReplaySource, "_STOP_QUIET_S", 0.2)
    t = threading.Thread(target=sdk_threads)
    t.start()
    src.stop()
    t.join()
    assert released and released[0] - binder.last_host_ns >= int(0.2e9)
    assert src.finished.is_set()
