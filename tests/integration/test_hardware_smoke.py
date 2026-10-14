"""Hardware-only checks. Run on the paired laptop with the glasses on the USB cable::

    ARIA_HARDWARE=1 pytest -m hardware tests/integration/test_hardware_smoke.py -s

``-s`` matters: with ``wifi_sap`` the test asks you to join the hotspot. Set
``ARIA_INTERFACE=usb`` for a tethered run that needs no interaction.
"""

from __future__ import annotations

import importlib.util
import os
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.hardware

DURATION_S = float(os.environ.get("ARIA_DURATION", "15"))
INTERFACE = os.environ.get("ARIA_INTERFACE", "wifi_sap")


def test_sdk_importable() -> None:
    assert importlib.util.find_spec("aria.sdk_gen2") is not None, (
        "pip install 'ariastreamlab[aria]'"
    )


@pytest.mark.slow
def test_live_session_then_replay(tmp_path: Path) -> None:
    """Connect, stream for ``DURATION_S``, check rates, the VRS, and replay the session."""
    from ariastreamlab import Experiment
    from ariastreamlab.config import load_dict
    from ariastreamlab.storage.manifest import Manifest

    cfg = load_dict(
        {
            "experiment": {"name": "hw_smoke", "participant": "P0"},
            "streams": {"rgb": {"fps": 10}, "imu": {}, "eye_gaze": {}, "vio": {}},
            "processors": [
                {"id": "mag", "type": "imu.magnitude", "inputs": ["imu_left"], "outputs": ["mag"]},
                {
                    "id": "gaze",
                    "type": "gaze.project_to_rgb",
                    "inputs": ["rgb", "eye_gaze"],
                    "outputs": ["gaze_rgb"],
                    "execution": {"join": "nearest", "tolerance_ms": 40},
                },
            ],
            "device": {"connection": {"streaming_interface": INTERFACE}},
            "storage": {"root": str(tmp_path / "sessions")},
            "dashboard": {"enabled": False},
        }
    )
    exp = Experiment(cfg, duration_s=DURATION_S)
    with exp:
        exp.mark("smoke_start")
        exp.run()
        stats = exp.stream_stats()
        pstats = exp.processor_stats()
        src_stats = exp.source.stats() if exp.source is not None else None
        root = exp.session_dir
    assert root is not None and src_stats is not None
    print({k: (v.received, v.effective_rate_hz) for k, v in stats.items()})
    print(src_stats.extra)
    assert stats["imu_left"].received > 0.9 * 800 * DURATION_S
    assert stats["rgb"].received >= 0.8 * 5 * DURATION_S  # profile9: RGB at 5 fps
    assert stats["eye_gaze"].received >= 0.5 * 25 * DURATION_S
    assert stats["device_calibration"].received == 1
    assert pstats["mag"].errors == 0 and pstats["mag"].outputs > 0
    assert pstats["gaze"].errors == 0 and pstats["gaze"].outputs > 0  # calibration arrived live
    assert src_stats.extra["callback_errors"] == {}
    m = Manifest.load(root / "manifest.json")
    assert m.status == "completed" and m.device.serial
    assert m.raw.kind == "host_received_vrs" and len(m.raw.files) == 1
    vrs = root / m.raw.files[0]
    assert vrs.stat().st_size > 1_000_000

    # the VRS opens and the session replays through the same pipeline
    from ariastreamlab.acquisition.aria_gen2 import vrs_replay

    provider = vrs_replay.open_provider(vrs)
    recorded = {s: provider.get_num_data(i) for s, i in vrs_replay.vrs_streams(provider).items()}
    assert recorded["imu_left"] > 0.9 * 800 * DURATION_S
    rep = Experiment.replay(root, speed=0.0)
    with rep:
        rep.run()
        rstats = rep.stream_stats()
        rproc = rep.processor_stats()
    assert rstats["imu_left"].received == recorded["imu_left"]
    assert rproc["mag"].outputs == recorded["imu_left"]
    counts = Counter({k: v.received for k, v in rstats.items()})
    assert counts["rgb"] == recorded["rgb"]
