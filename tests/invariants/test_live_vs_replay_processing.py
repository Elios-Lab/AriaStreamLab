"""Invariant I-1: processors cannot tell a live(-mock) run from a replay of its recording.

A mock session is recorded with a processing graph (stateful IMU filters, an image
processor, a two-input join). The session is then replayed through the *same* configuration
(its frozen ``experiment.json``) and every derived sample must match, value for value and
timestamp for timestamp. The only field allowed to differ is ``provenance``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ariastreamlab import Experiment
from ariastreamlab.core.sample import DerivedSample
from ariastreamlab.processing import Emit, Inputs, Processor
from ariastreamlab.storage.reader import SessionReader


class OriginSpy(Processor):
    """Records every provenance origin it sees (a *badly behaved* processor, for the test)."""

    seen: set[str] = set()

    def process(self, inputs: Inputs) -> Emit:
        OriginSpy.seen.add(inputs.primary.provenance.origin)
        return Emit("spy", float(inputs.primary.sequence_number or 0))


CONFIG = {
    "experiment": {"name": "inv", "participant": "P"},
    "streams": {"imu_left": {}, "rgb": {"fps": 10}, "eye_gaze": {}},
    "processors": [
        {
            "id": "lp",
            "type": "imu.lowpass",
            "inputs": ["imu_left"],
            "outputs": ["imu_lp"],
            "parameters": {"cutoff_hz": 3},
        },
        {"id": "mag", "type": "imu.magnitude", "inputs": ["imu_lp"], "outputs": ["mag"]},
        {
            "id": "move",
            "type": "imu.motion_detector",
            "inputs": ["imu_lp"],
            "outputs": ["moving"],
            "parameters": {"window_ms": 50},
        },
        {"id": "small", "type": "image.grayscale", "inputs": ["rgb"], "outputs": ["gray"]},
        {
            "id": "spy",
            "type": "tests.invariants.test_live_vs_replay_processing:OriginSpy",
            "inputs": ["imu_left"],
            "outputs": ["spy"],
        },
        {
            "id": "join",
            "type": "generic.passthrough",
            "inputs": ["rgb", "eye_gaze"],
            "outputs": ["rgb_copy"],
            "execution": {"join": "previous", "tolerance_ms": 100},
        },
    ],
    "mock": {"seed": 11, "jitter_ms": 0.0, "out_of_order_probability": 0.0, "realtime": False},
}


def _collect(root: Path) -> dict[str, list[DerivedSample]]:
    reader = SessionReader(root)
    out: dict[str, list[DerivedSample]] = {}
    for sid in ("imu_lp", "mag", "moving", "gray", "spy", "rgb_copy"):
        out[sid] = [s for s in reader.iter_stream(sid) if isinstance(s, DerivedSample)]
    return out


def test_derived_outputs_identical_between_mock_run_and_replay(tmp_path: Path) -> None:
    cfg_path = tmp_path / "inv.json"
    cfg = {**CONFIG, "storage": {"root": str(tmp_path / "sessions"), "image_format": "png"}}
    cfg_path.write_text(json.dumps(cfg))

    live = Experiment.from_json(cfg_path, mock=True, duration_s=1.0)
    with live:
        live.run()
        live_root = live.session_dir
        live_stats = live.processor_stats()
    assert live_root is not None
    assert OriginSpy.seen == {"mock"}
    assert all(s.errors == 0 for s in live_stats.values())
    assert live_stats["lp"].outputs == 800 and live_stats["small"].outputs == 10

    OriginSpy.seen = set()
    rep = Experiment.replay(live_root, speed=0.0)  # frozen experiment.json ⇒ same processors
    with rep:
        rep.run()
        rep_root = rep.session_dir
        rep_stats = rep.processor_stats()
    assert rep_root is not None and rep_root != live_root
    assert OriginSpy.seen == {"replay"}  # the spy is the only one who can tell
    assert all(s.errors == 0 for s in rep_stats.values())

    a, b = _collect(live_root), _collect(rep_root)
    for sid in a:
        assert len(a[sid]) == len(b[sid]) > 0, sid
        for x, y in zip(a[sid], b[sid], strict=True):
            assert x.timestamp_ns == y.timestamp_ns, sid
            assert x.processor_id == y.processor_id
            if isinstance(x.data, np.ndarray):
                assert np.array_equal(x.data, y.data), sid
            else:
                assert x.data == y.data, sid
        assert {s.provenance.origin for s in a[sid]} == {"replay"}  # read back from disk
    # the live session stored what the processors produced during the run
    assert len(a["imu_lp"]) == 800 and len(a["gray"]) == 10 and len(a["rgb_copy"]) == 10
