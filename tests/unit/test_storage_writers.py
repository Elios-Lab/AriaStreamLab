from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from ariastreamlab.core.errors import StorageError
from ariastreamlab.core.payloads import (
    AudioPayload,
    BarometerPayload,
    GazePayload,
    GpsPayload,
    HandPayload,
    ImuPayload,
    MagnetometerPayload,
    PosePayload,
    ScalarPayload,
)
from ariastreamlab.core.sample import DerivedSample, Provenance, Sample
from ariastreamlab.storage.schemas import SCHEMAS, payload_kind_of, row_to_payload, sample_to_row
from ariastreamlab.storage.writers import ImageSequenceWriter, ParquetChunkWriter, VideoWriter

PAYLOADS = {
    "imu": ImuPayload((1.0, 2.0, 3.0), (0.1, 0.2, 0.3), 27.5),
    "mag": MagnetometerPayload((1e-5, -2e-5, 3e-5)),
    "baro": BarometerPayload(101325.0, None),
    "gaze": GazePayload(0.1, -0.2, 1.5, True, 0.05, 0.15, -0.25, -0.15),
    "pose": PosePayload((1, 2, 3), (1, 0, 0, 0), (0.1, 0.2, 0.3), None, 0.9),
    "hand": HandPayload(np.zeros((21, 3), np.float32) + 0.5, None, 0.8, None),
    "audio": AudioPayload(
        np.arange(16, dtype=np.int16).reshape(4, 4), 16000, np.arange(4, dtype=np.int64) * 62500
    ),
    "scalar": ScalarPayload(3.5, "lux"),
    "gps": GpsPayload(44.4, 8.9, 30.0, 3.0, None, 1700000000000),
    "dict": {"a": 1, "b": [1, 2, 3], "c": "x"},
}


@pytest.mark.parametrize("kind", sorted(PAYLOADS))
def test_row_roundtrip_every_payload_kind(kind: str, provenance: Provenance) -> None:
    data = PAYLOADS[kind]
    assert payload_kind_of(data) == kind
    s = Sample("d", "x", 123, 456, 7, data, provenance)
    row = sample_to_row(s, kind)
    assert set(row) == set(SCHEMAS[kind].names)
    assert (row["device_timestamp_ns"], row["host_timestamp_ns"], row["sequence_number"]) == (
        123,
        456,
        7,
    )
    back = row_to_payload(row, kind)
    if kind == "hand":
        assert np.array_equal(back.left_landmarks_m, data.left_landmarks_m)
        assert back.right_landmarks_m is None
        assert back.left_confidence == 0.8
    elif kind == "audio":
        assert np.array_equal(back.samples, data.samples)
        assert np.array_equal(back.timestamps_ns, data.timestamps_ns)
        assert back.sample_rate_hz == 16000
    elif kind == "pose":
        assert back.translation_m == (1.0, 2.0, 3.0)
        assert back.angular_velocity_radsec is None
        assert back.linear_velocity_msec == pytest.approx((0.1, 0.2, 0.3))
    elif kind == "baro":
        assert back.temperature_c is None
        assert math.isnan(row["temperature_c"])
    else:
        assert back == data


def test_payload_kind_of_images_and_scalars() -> None:
    assert payload_kind_of(np.zeros((4, 4), np.uint8)) == "image"
    assert payload_kind_of(np.zeros((4, 4, 3), np.uint8)) == "image"
    assert payload_kind_of(2.5) == "scalar"
    assert payload_kind_of(np.float32(1)) == "scalar"
    assert payload_kind_of({"k": 1}) == "dict"
    assert payload_kind_of(np.zeros((4, 4), np.float32)) == "dict"


def _samples(n: int, provenance: Provenance, kind: str = "imu") -> list[Sample]:
    return [
        Sample("d", "s", 1_000_000 * i, 2_000_000 * i, i, PAYLOADS[kind], provenance)
        for i in range(n)
    ]


def test_parquet_chunk_writer_rotates_and_is_atomic(tmp_path: Path, provenance: Provenance) -> None:
    w = ParquetChunkWriter(tmp_path / "imu", "imu_left", "imu", chunk_seconds=1000, max_rows=4)
    for s in _samples(10, provenance):
        w.write(s)
    assert sorted(p.name for p in (tmp_path / "imu").glob("part-*.parquet")) == [
        "part-00000.parquet",
        "part-00001.parquet",
    ]
    assert not list((tmp_path / "imu").glob("*.tmp"))
    w.close()
    w.close()  # idempotent
    parts = sorted((tmp_path / "imu").glob("part-*.parquet"))
    assert len(parts) == 3
    total = sum(pq.read_metadata(p).num_rows for p in parts)
    assert total == 10
    assert w.stats.samples == 10
    assert w.stats.chunks == 3
    table = pq.read_table(parts[0])
    assert table.schema.names[:3] == ["device_timestamp_ns", "host_timestamp_ns", "sequence_number"]
    assert table.column("accel_x").to_pylist() == [1.0] * 4
    assert w.meta()["payload"] == "imu"
    # a new writer on the same directory continues numbering
    w2 = ParquetChunkWriter(tmp_path / "imu", "imu_left", "imu", chunk_seconds=1000)
    w2.write(_samples(1, provenance)[0])
    w2.close()
    assert (tmp_path / "imu" / "part-00003.parquet").exists()


def test_parquet_writer_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="no tabular schema"):
        ParquetChunkWriter(tmp_path, "x", "image", chunk_seconds=1)


def test_maybe_flush_respects_chunk_seconds(tmp_path: Path, provenance: Provenance) -> None:
    w = ParquetChunkWriter(tmp_path / "s", "s", "imu", chunk_seconds=0.0)
    w.write(_samples(1, provenance)[0])
    w.maybe_flush()
    assert w.stats.chunks == 1
    w.close()


def test_image_sequence_writer_png_lossless(tmp_path: Path, provenance: Provenance) -> None:
    import cv2

    w = ImageSequenceWriter(tmp_path / "rgb", "rgb", fmt="png", chunk_seconds=1000, max_rows=3)
    rng = np.random.default_rng(0)
    frames = [rng.integers(0, 255, (12, 16, 3), dtype=np.uint8) for _ in range(5)]
    for i, f in enumerate(frames):
        w.write(Sample("d", "rgb", 100 * i, 200 * i, i, f, provenance))
    w.close()
    files = sorted((tmp_path / "rgb").glob("frame-*.png"))
    assert len(files) == 5
    back = cv2.cvtColor(cv2.imread(str(files[2])), cv2.COLOR_BGR2RGB)
    assert np.array_equal(back, frames[2])
    idx = sorted((tmp_path / "rgb").glob("frames-*.parquet"))
    assert len(idx) == 2
    t = pq.read_table(idx[0])
    assert t.column("file").to_pylist()[0] == "frame-0000000.png"
    assert t.column("device_timestamp_ns").to_pylist() == [0, 100, 200]
    mono = ImageSequenceWriter(tmp_path / "slam", "slam", fmt="jpeg", chunk_seconds=1000)
    mono.write(Sample("d", "slam", 1, 2, 0, np.zeros((8, 8), np.uint8), provenance))
    mono.close()
    assert (tmp_path / "slam" / "frame-0000000.jpg").exists()
    assert mono.meta()["lossless"] is False


def test_video_writer_segments(tmp_path: Path, provenance: Provenance) -> None:
    import cv2

    w = VideoWriter(tmp_path / "rgb", "rgb", fps=10, chunk_seconds=1000, max_rows=4)
    frames = [np.full((16, 16, 3), i * 20, np.uint8) for i in range(6)]
    for i, f in enumerate(frames):
        w.write(Sample("d", "rgb", 100 * i, 200 * i, i, f, provenance))
    w.close()
    videos = sorted((tmp_path / "rgb").glob("video-*.mp4"))
    assert len(videos) == 2
    cap = cv2.VideoCapture(str(videos[0]))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    assert n == 4
    t = pq.read_table(sorted((tmp_path / "rgb").glob("frames-*.parquet"))[1])
    assert t.column("file").to_pylist() == ["video-00001.mp4"] * 2
    assert t.column("frame_index").to_pylist() == [0, 1]
    # size change forces a new segment
    w2 = VideoWriter(tmp_path / "rgb2", "rgb", fps=10, chunk_seconds=1000)
    w2.write(Sample("d", "rgb", 0, 0, 0, np.zeros((8, 8, 3), np.uint8), provenance))
    w2.write(Sample("d", "rgb", 1, 1, 1, np.zeros((16, 16, 3), np.uint8), provenance))
    w2.close()
    assert len(list((tmp_path / "rgb2").glob("video-*.mp4"))) == 2


def test_derived_sample_rows_use_processing_end(provenance: Provenance) -> None:
    d = DerivedSample("d", "out", 5, ("imu_left",), (5,), "p", 10, 12, 3.0, provenance)
    row = sample_to_row(d, "scalar")
    assert (row["host_timestamp_ns"], row["sequence_number"], row["value"]) == (12, -1, 3.0)
