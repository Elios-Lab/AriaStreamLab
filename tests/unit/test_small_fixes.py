"""Review §5: the smaller correctness issues, one regression test each."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import types
from datetime import timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import ConfigError, DeviceError
from ariastreamlab.core.payloads import ImuPayload
from ariastreamlab.core.sample import Provenance, Sample

# -- the aria_gen2 command line ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "paired"),
    [
        ("Device is authenticated", True),
        ("status: AUTHENTICATED\n", True),
        ("Device is not authenticated", False),
        ("unauthenticated", False),  # before: contains "authenticated" ⇒ True
        ("Un-authenticated client", False),
        ("authentication failed", False),
        ("", False),
    ],
)
def test_auth_check_output(text: str, paired: bool) -> None:
    from ariastreamlab.acquisition.aria_gen2.cli import is_authenticated

    assert is_authenticated(text) is paired


def test_a_missing_cli_is_a_device_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from ariastreamlab.acquisition.aria_gen2 import cli as cli_mod

    def missing(*_: Any, **__: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", "aria_gen2")

    monkeypatch.setattr(cli_mod.subprocess, "run", missing)
    with pytest.raises(DeviceError, match="cannot run"):
        cli_mod.AriaCli(executable="/removed/aria_gen2").run("device list")


def test_undecodable_cli_output_is_replaced_not_raised(tmp_path: Path) -> None:
    from ariastreamlab.acquisition.aria_gen2.cli import AriaCli

    script = tmp_path / "fake_cli"
    script.write_bytes(b"#!/bin/sh\nprintf 'ok \\377\\376 done'\n")
    script.chmod(0o755)
    res = AriaCli(executable=str(script)).run("device list")  # before: UnicodeDecodeError
    assert "ok" in res.text and "done" in res.text


@pytest.mark.parametrize(
    ("stdout", "joined"),
    [
        ("", True),
        ("Could not find network ariaHotspot.\n", False),  # exit 0 all the same
        ("Failed to join network ariaHotspot.\nError: -3900  The operation couldn't…", False),
    ],
)
def test_hotspot_auto_join_reads_the_output(
    stdout: str, joined: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ariastreamlab.acquisition.aria_gen2 import device as device_mod

    monkeypatch.setattr(
        device_mod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=stdout, stderr=""),
    )
    assert device_mod._macos_join_wifi("ariaHotspot", "secret") is joined


# -- the bus ---------------------------------------------------------------------------------


def test_publish_counters_are_exact_under_contention(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr(sys, "getswitchinterval", sys.getswitchinterval)  # restored below
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads as often as possible: the race shows
    monkeypatch.undo()
    bus = EventBus()
    n_threads, per_thread = 8, 20_000

    def publish() -> None:
        for _ in range(per_thread):
            bus.publish("raw.imu_left", None)

    threads = [threading.Thread(target=publish) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sys.setswitchinterval(previous)
    assert bus.published == n_threads * per_thread
    assert bus.topics()["raw.imu_left"] == n_threads * per_thread


def test_an_empty_subscription_is_truthy() -> None:
    bus = EventBus()
    sub = bus.subscribe("raw.*", maxsize=4)
    assert len(sub) == 0 and sub  # `if sub:` means "there is a subscription"
    bus.close()


# -- timing ------------------------------------------------------------------------------------


def test_interpolate_a_buffer_of_imu_samples() -> None:
    from ariastreamlab.timing.sync import TimeSeriesBuffer, interp_imu

    prov = Provenance("mock", "mock", "s")
    buf: TimeSeriesBuffer[Sample] = TimeSeriesBuffer()
    for ts, ax in ((0, 0.0), (100, 10.0)):
        buf.push(
            Sample("d", "imu_left", ts, ts, None, ImuPayload((ax, 0, 0), (0, 0, 0), None), prov)
        )
    out = buf.interpolate(25, interp_imu)  # before: AttributeError ('Sample' has no accel)
    assert out is not None and out.accel_msec2[0] == pytest.approx(2.5)


@pytest.mark.parametrize(
    ("ns", "us"), [(1_500, 1), (-1_500, -1), (-999, 0), (-1_000, -1), (2_000_999, 2_000)]
)
def test_ns_to_timedelta_truncates_toward_zero(ns: int, us: int) -> None:
    from ariastreamlab.timing.convert import ns_to_timedelta

    assert ns_to_timedelta(ns) == timedelta(microseconds=us)


# -- the reader ------------------------------------------------------------------------------


def _parts(directory: Path, chunks: list[list[int]]) -> Any:
    from ariastreamlab.storage.reader import StoredStream

    directory.mkdir(parents=True)
    for i, ts in enumerate(chunks):
        pq.write_table(pa.table({"device_timestamp_ns": ts}), directory / f"part-{i:05d}.parquet")
    return StoredStream("imu_left", "raw", directory, {})


def _reader() -> Any:
    from ariastreamlab.storage.reader import SessionReader

    r = SessionReader.__new__(SessionReader)
    r.read_errors = []
    return r


def test_rows_are_in_time_order_across_chunk_boundaries(tmp_path: Path) -> None:
    # each chunk sorted, but a late sample landed in the next chunk (flush boundary)
    st = _parts(tmp_path / "s", [[10, 20, 30, 50], [40, 60, 70], [65, 80]])
    rows = [r["device_timestamp_ns"] for r in _reader()._rows(st, "part-*.parquet", None, None)]
    assert rows == [10, 20, 30, 40, 50, 60, 65, 70, 80]  # before: 50 before 40, 70 before 65


def test_an_unreadable_chunk_is_reported(tmp_path: Path) -> None:
    st = _parts(tmp_path / "s", [[1, 2], [3]])
    (tmp_path / "s" / "part-00001.parquet").write_bytes(b"not parquet")
    reader = _reader()
    rows = [r["device_timestamp_ns"] for r in reader._rows(st, "part-*.parquet", None, None)]
    assert rows == [1, 2]
    assert len(reader.read_errors) == 1 and "part-00001.parquet" in reader.read_errors[0]


def test_video_frames_are_read_without_a_seek_per_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cv2

    from ariastreamlab import Experiment
    from ariastreamlab.config import load_dict
    from ariastreamlab.storage.reader import SessionReader

    cfg = load_dict(
        {
            "experiment": {"name": "video", "participant": "P1"},
            "storage": {"root": str(tmp_path / "sessions"), "image_format": "mp4"},
            "streams": {"rgb": {"fps": 10}},
            "mock": {"seed": 1, "jitter_ms": 0.0, "realtime": False},
            "dashboard": {"enabled": False},
        }
    )
    with Experiment(cfg, mock=True, duration_s=2.0) as exp:
        exp.run()
    real = cv2.VideoCapture
    seeks: list[int] = []

    class Counting:
        def __init__(self, path: str) -> None:
            self._cap = real(path)

        def set(self, prop: int, value: float) -> bool:
            seeks.append(int(value))
            return bool(self._cap.set(prop, value))

        def __getattr__(self, name: str) -> Any:
            return getattr(self._cap, name)

    monkeypatch.setattr(cv2, "VideoCapture", Counting)
    frames = list(SessionReader(exp.session_dir).iter_stream("rgb"))  # type: ignore[arg-type]
    assert len(frames) == 20 and all(f.data is not None for f in frames)
    assert seeks == []  # before: one seek per frame


# -- statistics ------------------------------------------------------------------------------


def test_many_missing_sequence_numbers_do_not_slow_every_gap_down() -> None:
    from ariastreamlab.monitoring import stats as stats_mod

    cap = stats_mod._MAX_MISSING
    t = stats_mod._StreamTracker(None, 5.0)
    n = cap + 10_000
    t0 = time.perf_counter()
    for i in range(n):  # every other sequence number lost: 10 000 gaps over the cap
        t._observe_seq(2 * i)
    # before: the whole set was sorted on every gap, under the statistics lock (≈ 5 s here
    # for 10 000 gaps); now ≈ 0.05 s
    assert time.perf_counter() - t0 < 1.5
    assert len(t.missing) == cap
    assert t.confirmed_dropped + len(t.missing) == n - 1
    assert min(t.missing) == 2 * (n - 1 - cap) + 1  # the oldest holes were evicted
    t._observe_seq(39_997)  # a late arrival still fills its hole
    assert 39_997 not in t.missing


# -- the SDK replay's image decoding -----------------------------------------------------


def test_sdk_replay_forwards_only_decoded_frames() -> None:
    from ariastreamlab.acquisition.aria_gen2.vrs_replay import _DecodingHandler

    registered: dict[str, Any] = {}
    handler = types.SimpleNamespace(
        register_rgb_callback=lambda cb: registered.__setitem__("rgb", cb),
        register_imu_callback=lambda cb: registered.__setitem__("imu", cb),
    )

    class Decoder:
        def decode_image(self, image: Any, record: Any) -> bool:
            if record == "corrupt":
                return False
            image["decoded"] = True
            return True

    proxy = _DecodingHandler(handler, Decoder())
    got: list[Any] = []
    proxy.register_rgb_callback(lambda image, record: got.append(image))
    proxy.register_imu_callback("imu-cb")  # anything else goes straight through
    assert registered["imu"] == "imu-cb"
    registered["rgb"]({"decoded": False}, "ok")
    registered["rgb"]({"decoded": False}, "corrupt")
    assert got == [{"decoded": True}]  # the raw H.265 frame is never passed on
    assert proxy.decode_failed["rgb"] == 1


# -- the designer and `design` ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "message"), [(None, "no experiment file"), ("{", "not valid JSON")]
)
def test_design_with_an_unreadable_file_exits_cleanly(
    content: str | None, message: str, tmp_path: Path
) -> None:
    from ariastreamlab.cli.main import app

    path = tmp_path / "experiment.json"
    if content is not None:
        path.write_text(content)
    res = CliRunner().invoke(app, ["design", str(path), "--no-open"])
    assert res.exit_code == 2  # like `run`; before: a traceback and exit 1
    assert message in res.output and "Traceback" not in res.output


def test_designer_load_errors_are_config_errors(tmp_path: Path) -> None:
    from ariastreamlab.dashboard.designer import Designer

    (tmp_path / "list.json").write_text("[1, 2]")
    with pytest.raises(ConfigError, match="JSON object"):
        Designer.from_file(tmp_path / "list.json")


def test_saving_keeps_extends_and_writes_only_the_changes(tmp_path: Path) -> None:
    from ariastreamlab.config import load_config
    from ariastreamlab.dashboard.designer import Designer

    src = tmp_path / "study.json"
    src.write_text(
        json.dumps(
            {
                "extends": "preset:rgb_imu",
                "experiment": {"name": "s"},
                "streams": {"rgb": {"fps": 5}},
            }
        )
    )
    d = Designer.from_file(src)
    d.doc["experiment"]["participant"] = "P7"
    saved = json.loads(d.save("study.json").read_text())
    assert saved["extends"] == "preset:rgb_imu"  # before: flattened into the preset's values
    assert saved["streams"] == {"rgb": {"fps": 5}}
    assert saved["experiment"] == {"name": "s", "participant": "P7"}
    cfg = load_config(src)
    assert cfg.experiment.participant == "P7" and cfg.enabled_streams()["rgb"]["fps"] == 5


def test_save_as_into_another_folder_keeps_relative_paths_pointing_where_they_did(
    tmp_path: Path,
) -> None:
    from ariastreamlab.config import load_config
    from ariastreamlab.dashboard.designer import Designer

    project = tmp_path / "project"
    project.mkdir()
    src = project / "experiment.json"
    src.write_text(
        json.dumps(
            {"experiment": {"name": "s"}, "streams": {"imu": {}}, "storage": {"root": "data"}}
        )
    )
    d = Designer.from_file(src)
    copy = d.save(tmp_path / "elsewhere" / "copy.json")
    assert json.loads(copy.read_text())["storage"]["root"] == "../project/data"
    assert load_config(copy).storage.root == load_config(src).storage.root == project / "data"
    assert d.path == copy


def test_the_default_sessions_folder_is_next_to_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ariastreamlab.config import load_config

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    f = tmp_path / "a" / "experiment.json"
    f.write_text(json.dumps({"experiment": {"name": "s"}, "streams": {"imu": {}}}))
    monkeypatch.chdir(tmp_path / "b")
    assert load_config(f).storage.root == tmp_path / "a" / "sessions"  # before: b/sessions
