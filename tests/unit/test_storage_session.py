from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from ariastreamlab.config import load_dict
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import InvariantError, StorageError
from ariastreamlab.core.payloads import ImuPayload
from ariastreamlab.core.sample import DerivedSample, Provenance, Sample
from ariastreamlab.events.markers import DeviceTimeEstimator, EventLog
from ariastreamlab.monitoring.stats import StatsTracker
from ariastreamlab.storage.derived import DerivedRecorder
from ariastreamlab.storage.events_store import read_events
from ariastreamlab.storage.inspect import inspect_session, summary_as_dict
from ariastreamlab.storage.layout import SessionLayout
from ariastreamlab.storage.manifest import Manifest, manifest_status
from ariastreamlab.storage.reader import SessionReader
from ariastreamlab.storage.session import Session, SessionInfo, session_folder_name
from ariastreamlab.storage.timeline import read_timeline, write_timeline


def _config(root: Path, **extra: object) -> object:
    data: dict[str, object] = {
        "experiment": {"name": "store_test", "participant": "P2"},
        "streams": {"rgb": {"fps": 10}, "imu": {}},
        "processors": [
            {
                "id": "m",
                "type": "imu.motion_detector",
                "inputs": ["imu_left"],
                "outputs": ["head_motion"],
                "save_output": True,
            },
            {
                "id": "n",
                "type": "x",
                "inputs": ["rgb"],
                "outputs": ["unsaved"],
                "save_output": False,
            },
        ],
        "storage": {"root": str(root), "image_format": "png", "chunk_seconds": 1000},
    }
    data.update(extra)
    return load_dict(data)


def test_folder_name_and_info() -> None:
    info = SessionInfo.create("my exp/1", "P 1", "mock")
    assert info.folder_name.endswith("_my-exp-1_P-1")
    assert len(info.session_id) == 36
    from datetime import datetime, timezone

    assert (
        session_folder_name(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), "a", "b")
        == "20260102T030405Z_a_b"
    )


def test_layout_create_freeze_and_raw_guard(tmp_path: Path) -> None:
    layout = SessionLayout(tmp_path / "s")
    layout.create()
    for d in (
        layout.raw,
        layout.derived,
        layout.events,
        layout.indexes,
        layout.logs,
        layout.calibration,
        layout.quality,
    ):
        assert d.is_dir()
    with pytest.raises(StorageError, match="already exists"):
        layout.create()
    sha = layout.freeze_config('{"a": 1}', '{"src": true}')
    assert len(sha) == 64
    assert not (os.stat(layout.experiment).st_mode & stat.S_IWUSR)
    assert layout.experiment_source.read_text() == '{"src": true}'
    with pytest.raises(InvariantError, match="raw/"):
        layout.guard_raw_write(layout.raw / "x.parquet")
    layout.guard_raw_write(layout.derived / "x")  # fine
    with pytest.raises(StorageError, match="not a session directory"):
        SessionLayout.open_existing(tmp_path / "nope")


def test_session_open_finalize_and_inspect(tmp_path: Path, provenance: Provenance) -> None:
    cfg = _config(tmp_path / "sessions")
    info = SessionInfo.create("store_test", "P2", "mock")
    bus = EventBus()
    sess = Session(
        info, cfg, root=tmp_path / "sessions", source_json='{"experiment": {"name": "store_test"}}'
    )  # type: ignore[arg-type]
    sess.open(bus, raw_streams_to_file=["imu_left", "rgb"])
    assert sess.layout is not None
    root = sess.layout.root
    assert root.name == info.folder_name
    m = Manifest.load(root / "manifest.json")
    assert m.status == "running"
    assert m.enabled_streams == ["rgb", "imu_left", "imu_right"]
    assert [p["id"] for p in m.processors] == ["m", "n"]
    assert manifest_status(root) == "running"
    # publish raw + derived samples
    for i in range(50):
        bus.publish(
            "raw.imu_left",
            Sample(
                "mock-0",
                "imu_left",
                1_000_000 * i,
                1_000_000 * i + 5,
                i,
                ImuPayload((0, 0, 9.8), (0, 0, 0)),
                provenance,
            ),
        )
    for i in range(3):
        bus.publish(
            "raw.rgb",
            Sample(
                "mock-0",
                "rgb",
                10_000_000 * i,
                10_000_000 * i + 5,
                i,
                np.full((8, 8, 3), i, np.uint8),
                provenance,
            ),
        )
        bus.publish(
            "derived.head_motion",
            DerivedSample(
                "mock-0",
                "head_motion",
                10_000_000 * i,
                ("imu_left",),
                (10_000_000 * i,),
                "m",
                1,
                2,
                float(i),
                provenance,
            ),
        )
        bus.publish(
            "derived.unsaved",
            DerivedSample(
                "mock-0", "unsaved", 10_000_000 * i, ("rgb",), (), "n", 1, 2, 1.0, provenance
            ),
        )
    deadline = time.monotonic() + 5
    while (
        time.monotonic() < deadline and sum(x.samples for x in sess.recorder.stats().values()) < 56
    ):
        time.sleep(0.01)
    stats = StatsTracker()
    events = EventLog(bus, DeviceTimeEstimator())
    events.mark("trial_start", {"trial": 1})
    ended = sess.finalize(
        status="completed",
        events=events,
        stream_stats=stats.snapshot_all(),
        ended_utc_ns=time.time_ns(),
        extra={"device": {"serial": "SER1"}, "source": {"received": {"imu_left": 50}}},
    )
    assert ended == root
    m = Manifest.load(root / "manifest.json")
    assert m.status == "completed"
    assert m.device.serial == "SER1"
    assert m.events == 1
    assert m.duration_s is not None
    assert (root / "logs" / "session.log").exists()
    # files on disk
    assert pq.read_metadata(root / "raw_mock" / "imu_left" / "part-00000.parquet").num_rows == 50
    assert len(list((root / "raw_mock" / "rgb").glob("frame-*.png"))) == 3
    assert pq.read_metadata(root / "derived" / "head_motion" / "part-00000.parquet").num_rows == 3
    assert not (root / "derived" / "unsaved").exists()
    meta = json.loads((root / "derived" / "head_motion" / "_meta.json").read_text())
    assert meta["payload"] == "scalar"
    assert meta["samples"] == 3
    evs = read_events(root / "events" / "events.parquet")
    assert [e.name for e in evs] == ["trial_start"]
    assert evs[0].payload == {"trial": 1}
    tl = read_timeline(sess.layout)
    assert tl.num_rows == 56
    assert set(tl.column("stream_id").to_pylist()) == {"imu_left", "rgb", "head_motion"}
    # reader
    reader = SessionReader(root)
    assert set(reader.streams) == {"imu_left", "rgb", "head_motion"}
    imu = list(reader.iter_stream("imu_left"))
    assert len(imu) == 50
    assert isinstance(imu[10], Sample)
    assert imu[10].device_timestamp_ns == 10_000_000
    assert imu[10].sequence_number == 10
    assert imu[10].data == ImuPayload((0.0, 0.0, 9.8), (0.0, 0.0, 0.0), None)
    assert imu[10].device_id == "SER1"
    assert imu[10].provenance.origin == "replay"
    rgb = list(reader.iter_stream("rgb"))
    assert len(rgb) == 3
    assert np.array_equal(rgb[2].data, np.full((8, 8, 3), 2, np.uint8))
    hm = list(reader.iter_stream("head_motion"))
    assert isinstance(hm[0], DerivedSample)
    assert hm[0].processor_id == "unknown" or hm[0].processor_id == "m"
    merged = list(reader.iter_all(["imu_left", "rgb"]))
    assert [s.device_timestamp_ns for s in merged] == sorted(s.device_timestamp_ns for s in merged)
    assert reader.time_range(["imu_left"]) == (0, 49_000_000)
    windowed = list(reader.iter_stream("imu_left", start_ns=10_000_000, end_ns=12_000_000))
    assert [s.sequence_number for s in windowed] == [10, 11, 12]
    with pytest.raises(StorageError, match="not stored"):
        list(reader.iter_stream("ghost"))
    # inspect
    summary = inspect_session(root)
    assert summary.manifest.status == "completed"
    assert {s.stream_id: s.samples for s in summary.streams} == {
        "imu_left": 50,
        "rgb": 3,
        "head_motion": 3,
    }
    assert summary.problems == []
    d = summary_as_dict(summary)
    assert d["events"] == 1
    assert d["timeline_rows"] == 56
    bus.close()


def test_crash_recovery_marks_crashed_and_rebuilds_timeline(
    tmp_path: Path, provenance: Provenance
) -> None:
    cfg = _config(tmp_path / "sessions")
    info = SessionInfo.create("store_test", "P2", "mock")
    bus = EventBus()
    sess = Session(info, cfg, root=tmp_path / "sessions")  # type: ignore[arg-type]
    sess.open(bus, raw_streams_to_file=["imu_left"])
    assert sess.recorder is not None and sess.layout is not None
    for i in range(5):
        bus.publish(
            "raw.imu_left",
            Sample("mock-0", "imu_left", i, i, i, ImuPayload((0, 0, 0), (0, 0, 0)), provenance),
        )
    deadline = time.monotonic() + 5
    while (
        time.monotonic() < deadline and sum(x.samples for x in sess.recorder.stats().values()) < 5
    ):
        time.sleep(0.01)
    # simulate a crash: flush what the writer has, but never finalize
    for w in sess.recorder._writers.values():
        w.flush()
    root = sess.layout.root
    (root / "raw_mock" / "imu_left" / "part-00001.parquet").write_bytes(
        b"PAR1garbage"
    )  # truncated chunk
    assert manifest_status(root) == "running"
    summary = inspect_session(root)
    assert any("running" in p for p in summary.problems)
    assert any("_meta.json missing" in p for p in summary.problems)
    assert any("unreadable chunk" in p for p in summary.problems)
    assert {s.stream_id: s.samples for s in summary.streams} == {"imu_left": 5}
    repaired = inspect_session(root, repair=True)
    assert repaired.repaired
    assert Manifest.load(root / "manifest.json").status == "crashed"
    assert repaired.timeline_rows == 5
    reader = SessionReader(root)
    assert len(list(reader.iter_stream("imu_left"))) == 5  # the garbage chunk is skipped
    sess.recorder.close()
    bus.close()


def test_recorder_ignores_unlisted_streams_and_refuses_raw_dir(
    tmp_path: Path, provenance: Provenance
) -> None:
    cfg = _config(tmp_path / "sessions")
    layout = SessionLayout(tmp_path / "s")
    layout.create()
    bus = EventBus()
    rec = DerivedRecorder(bus, layout, cfg.storage, derived_streams=["a"], raw_streams=["imu_left"])  # type: ignore[attr-defined]
    rec.start()
    bus.publish("derived.a", DerivedSample("d", "a", 1, (), (), "p", 1, 2, 1.0, provenance))
    bus.publish("derived.b", DerivedSample("d", "b", 1, (), (), "p", 1, 2, 1.0, provenance))
    bus.publish(
        "raw.imu_right",
        Sample("d", "imu_right", 1, 1, 0, ImuPayload((0, 0, 0), (0, 0, 0)), provenance),
    )
    time.sleep(0.2)
    rec.close()
    assert set(rec.stats()) == {"a"}
    assert write_timeline(layout) == 1
    bus.close()
