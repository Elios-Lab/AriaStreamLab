from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import BaseModel

from ariastreamlab.config import load_dict
from ariastreamlab.config.models import ExecutionConfig, ProcessorConfig
from ariastreamlab.core.bus import EventBus
from ariastreamlab.core.errors import ConfigError, ProcessingError
from ariastreamlab.core.payloads import GazePayload, ImuPayload, ScalarPayload
from ariastreamlab.core.registry import REGISTRY
from ariastreamlab.core.sample import DerivedSample, Provenance, Sample
from ariastreamlab.processing import (
    BUILTINS,
    Emit,
    Inputs,
    ProcessingGraph,
    Processor,
    ProcessorContext,
    ProcessorWorker,
    list_processors,
    resolve,
    validate_processors,
)
from ariastreamlab.processing.builtin.gaze import fisheye624_project, gaze_point_cpf

# --------------------------------------------------------------------------- helpers


class Doubler(Processor):
    """Multiplies a scalar input by `factor`."""

    version = "3"

    class Parameters(BaseModel):
        factor: float = 2.0

    def process(self, inputs: Inputs) -> Emit:
        return Emit("doubled", float(inputs.primary.data) * self.params.factor)


class Joiner(Processor):
    def process(self, inputs: Inputs) -> tuple[str, Any, dict[str, Any]]:
        return ("joined", {k: s.device_timestamp_ns for k, s in inputs.items()}, dict(inputs.dt_ns))


class Flaky(Processor):
    def process(self, inputs: Inputs) -> Emit | None:
        if inputs.primary.sequence_number == 2:
            raise RuntimeError("boom")
        if inputs.primary.sequence_number == 3:
            return Emit("undeclared", 1.0)
        return Emit("ok", 1.0)


class Multi(Processor):
    def process(self, inputs: Inputs) -> list[Emit]:
        v = float(inputs.primary.data)
        return [Emit("a", v, timestamp_ns=inputs.primary.device_timestamp_ns + 5), Emit("b", -v)]


def _cfg(pid: str, type_: str, inputs: list[str], outputs: list[str], **kw: Any) -> ProcessorConfig:
    # a roomy queue by default: these tests publish in a tight loop and want every sample
    ex = {"queue_size": 1024, **kw.pop("execution", {})}
    return ProcessorConfig(
        id=pid, type=type_, inputs=inputs, outputs=outputs, execution=ExecutionConfig(**ex), **kw
    )


def _ctx(cfg: ProcessorConfig, params: Any = None, **kw: Any) -> ProcessorContext:
    return ProcessorContext(
        cfg.id,
        params if params is not None else dict(cfg.parameters),
        tuple(cfg.inputs),
        tuple(cfg.outputs),
        REGISTRY,
        **kw,
    )


def _wait(pred, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.005)


# --------------------------------------------------------------------------- worker


def test_worker_single_input_emits_derived_samples(bus: EventBus, make_sample) -> None:
    cfg = _cfg("d", "x", ["imu_left"], ["doubled"], parameters={"factor": 3})
    out = bus.subscribe("derived.doubled", maxsize=100)
    w = ProcessorWorker(cfg, Doubler(), _ctx(cfg, Doubler.Parameters(factor=3)), bus)
    w.start()
    for i in range(5):
        bus.publish(
            "raw.imu_left", make_sample(device_ns=100 * i, host_ns=100 * i + 1, seq=i, data=i)
        )
    _wait(lambda: w.stats().fired == 5)
    w.stop()
    items = out.drain()
    assert [s.data for s in items] == [0.0, 3.0, 6.0, 9.0, 12.0]
    d = items[2]
    assert isinstance(d, DerivedSample)
    assert d.stream_id == "doubled"
    assert d.timestamp_ns == 200
    assert d.source_streams == ("imu_left",)
    assert d.source_timestamps_ns == (200,)
    assert d.processor_id == "d"
    assert d.processing_end_ns >= d.processing_start_ns
    assert d.provenance.origin == "mock"
    assert d.device_id == "mock-0"
    st = w.stats()
    assert st.state == "stopped" and st.outputs == 5 and st.errors == 0
    assert st.exec_ms_mean is not None and st.latency_ms_mean is not None
    assert st.inputs_received == {"imu_left": 5}


def test_worker_isolates_errors_and_rejects_undeclared_outputs(bus: EventBus, make_sample) -> None:
    cfg = _cfg("f", "x", ["imu_left"], ["ok"])
    out = bus.subscribe("derived.*", maxsize=100)
    w = ProcessorWorker(cfg, Flaky(), _ctx(cfg), bus)
    w.start()
    for i in range(5):
        bus.publish("raw.imu_left", make_sample(device_ns=i, seq=i))
    _wait(lambda: w.stats().inputs_received.get("imu_left") == 5 and w.stats().outputs == 3)
    w.stop()
    assert len(out.drain()) == 3
    st = w.stats()
    assert st.errors == 2
    assert st.last_error is not None and "undeclared" in st.last_error
    assert st.state == "stopped"


def test_worker_multiple_outputs_tuples_and_custom_timestamp(bus: EventBus, make_sample) -> None:
    cfg = _cfg("m", "x", ["imu_left"], ["a", "b"])
    out = bus.subscribe("derived.*", maxsize=100)
    w = ProcessorWorker(cfg, Multi(), _ctx(cfg), bus)
    w.start()
    bus.publish("raw.imu_left", make_sample(device_ns=1000, data=2.5))
    _wait(lambda: w.stats().outputs == 2)
    w.stop()
    items = {s.stream_id: s for s in out.drain()}
    assert items["a"].timestamp_ns == 1005 and items["a"].data == 2.5
    assert items["b"].timestamp_ns == 1000 and items["b"].data == -2.5


def test_worker_drop_policy_latest_keeps_up(bus: EventBus, make_sample) -> None:
    class Slow(Processor):
        def process(self, inputs: Inputs) -> Emit:
            time.sleep(0.02)
            return Emit("o", inputs.primary.sequence_number)

    cfg = _cfg("s", "x", ["rgb"], ["o"], execution={"queue_size": 1, "drop_policy": "latest"})
    out = bus.subscribe("derived.o", maxsize=1000)
    w = ProcessorWorker(cfg, Slow(), _ctx(cfg), bus)
    w.start()
    for i in range(40):
        bus.publish("raw.rgb", make_sample("rgb", device_ns=i, seq=i))
    time.sleep(0.05)
    w.stop()
    seqs = [s.data for s in out.drain()]
    assert seqs == sorted(seqs)
    assert seqs[-1] == 39  # the newest sample is always processed on stop
    st = w.stats()
    assert st.queue_dropped.get("rgb", 0) > 0
    assert st.fired == len(seqs) < 40


@pytest.mark.parametrize("join", ["latest", "previous", "nearest"])
def test_worker_join_policies(join: str, bus: EventBus, make_sample) -> None:
    cfg = _cfg(
        "j",
        "x",
        ["rgb", "eye_gaze"],
        ["joined"],
        execution={"join": join, "tolerance_ms": 10, "queue_size": 16},
    )
    out = bus.subscribe("derived.joined", maxsize=100)
    w = ProcessorWorker(cfg, Joiner(), _ctx(cfg), bus)
    w.start()
    ms = 1_000_000
    for t in (0, 5, 12, 20, 33):  # gaze samples
        bus.publish("raw.eye_gaze", make_sample("eye_gaze", device_ns=t * ms, seq=t))
    time.sleep(0.05)
    bus.publish("raw.rgb", make_sample("rgb", device_ns=14 * ms, seq=0))  # frame at 14 ms
    _wait(lambda: w.stats().fired + w.stats().join_misses >= 1)
    if join == "nearest":
        # a frame whose closest gaze is still to come fires once a later gaze exists…
        bus.publish("raw.rgb", make_sample("rgb", device_ns=40 * ms, seq=1))
        bus.publish("raw.eye_gaze", make_sample("eye_gaze", device_ns=41 * ms, seq=41))
        bus.publish("raw.eye_gaze", make_sample("eye_gaze", device_ns=60 * ms, seq=60))
        _wait(lambda: w.stats().fired >= 2)
        # …and one far from every gaze sample is a join miss
        bus.publish("raw.rgb", make_sample("rgb", device_ns=200 * ms, seq=2))
        _wait(lambda: w.stats().join_misses >= 1)
    w.stop()
    items = out.drain()
    first = items[0]
    assert first.source_streams == ("rgb", "eye_gaze")
    if join == "latest":
        assert first.data["eye_gaze"] == 33 * ms and first.metadata["eye_gaze"] == 19 * ms
    elif join == "previous":
        assert first.data["eye_gaze"] == 12 * ms and first.metadata["eye_gaze"] == -2 * ms
    else:
        assert first.data["eye_gaze"] == 12 * ms  # |14-12| < |20-14|
        assert items[1].data["eye_gaze"] == 41 * ms
        assert w.stats().join_misses == 1


def test_worker_nearest_fires_after_host_wait_without_later_sample(
    bus: EventBus, make_sample
) -> None:
    cfg = _cfg(
        "j", "x", ["rgb", "eye_gaze"], ["joined"], execution={"join": "nearest", "tolerance_ms": 5}
    )
    out = bus.subscribe("derived.joined", maxsize=10)
    w = ProcessorWorker(cfg, Joiner(), _ctx(cfg), bus)
    w.start()
    bus.publish("raw.eye_gaze", make_sample("eye_gaze", device_ns=10_000_000, seq=0))
    bus.publish("raw.rgb", make_sample("rgb", device_ns=12_000_000, seq=0))
    _wait(lambda: w.stats().fired == 1, timeout=2)
    assert w.stats().pending == 0
    w.stop()
    assert out.drain()[0].data["eye_gaze"] == 10_000_000


def test_worker_setup_error_propagates(bus: EventBus) -> None:
    class Bad(Processor):
        def setup(self, ctx: ProcessorContext) -> None:
            raise ValueError("no way")

        def process(self, inputs: Inputs) -> None:
            return None

    cfg = _cfg("b", "x", ["imu_left"], ["o"])
    w = ProcessorWorker(cfg, Bad(), _ctx(cfg), bus)
    with pytest.raises(ValueError, match="no way"):
        w.start()
    assert w.stats().state == "failed"


def test_worker_receives_calibration(bus: EventBus, make_sample) -> None:
    seen: list[dict[str, Any]] = []

    class Calib(Processor):
        def on_calibration(self, calibration: dict[str, Any]) -> None:
            seen.append(calibration)

        def process(self, inputs: Inputs) -> None:
            return None

    cfg = _cfg("c", "x", ["imu_left"], ["o"])
    w = ProcessorWorker(cfg, Calib(), _ctx(cfg, calibration={"pre": 1}), bus)
    w.start()
    bus.publish("raw.device_calibration", make_sample("device_calibration", data={"cameras": {}}))
    bus.publish("raw.imu_left", make_sample())
    _wait(lambda: len(seen) == 2)
    w.stop()
    assert seen == [{"pre": 1}, {"cameras": {}}]
    assert w.ctx.calibration == {"cameras": {}}


# --------------------------------------------------------------------------- plugins


def test_builtins_resolve_and_list() -> None:
    for name in BUILTINS:
        cls = resolve(name)
        assert issubclass(cls, Processor)
    infos = {i.name: i for i in list_processors()}
    assert set(BUILTINS) <= set(infos)
    assert infos["image.resize"].origin == "builtin"
    assert infos["image.resize"].description


def test_resolve_module_class_and_errors() -> None:
    assert resolve("tests.unit.test_processing:Doubler") is Doubler
    with pytest.raises(ProcessingError, match="unknown processor type"):
        resolve("nope.nothing")
    with pytest.raises(ProcessingError, match="cannot import module"):
        resolve("no_such_module_xyz:Thing")
    with pytest.raises(ProcessingError, match="has no class"):
        resolve("tests.unit.test_processing:Missing")
    with pytest.raises(ProcessingError, match="not a Processor subclass"):
        resolve("tests.unit.test_processing:_cfg")


def test_resolve_from_file(tmp_path: Path) -> None:
    (tmp_path / "mine.py").write_text(
        "from ariastreamlab.processing import Processor, Inputs, Emit\n"
        "class Mine(Processor):\n"
        "    def process(self, inputs: Inputs):\n"
        "        return Emit('o', 1.0)\n"
    )
    (tmp_path / "two.py").write_text(
        "from ariastreamlab.processing import Processor, Inputs, Emit\n"
        "class A(Processor):\n"
        "    def process(self, inputs): return None\n"
        "class B(Processor):\n"
        "    def process(self, inputs): return None\n"
    )
    (tmp_path / "broken.py").write_text("raise RuntimeError('syntax-ish')\n")
    assert resolve("mine", tmp_path).__name__ == "Mine"
    assert resolve("mine.py", tmp_path).__name__ == "Mine"
    assert resolve("mine.py:Mine", tmp_path).__name__ == "Mine"
    assert resolve("two:B", tmp_path).__name__ == "B"
    with pytest.raises(ProcessingError, match="defines 2 Processor classes"):
        resolve("two", tmp_path)
    with pytest.raises(ProcessingError, match="error while importing"):
        resolve("broken", tmp_path)
    with pytest.raises(ProcessingError, match="not found"):
        resolve("ghost.py", tmp_path)
    names = {i.name for i in list_processors(tmp_path)}
    assert {"mine.py:Mine", "two.py:A", "two.py:B"} <= names


def test_entry_point_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    class FakeEp:
        name = "ext.doubler"

        def load(self) -> type[Processor]:
            return Doubler

    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda group=None: [FakeEp()] if group else []
    )
    assert resolve("ext.doubler") is Doubler
    assert any(i.name == "ext.doubler" and i.origin == "entry_point" for i in list_processors())


# --------------------------------------------------------------------------- graph


def _graph_config(root: Path, processors: list[dict[str, Any]], **storage: Any):
    return load_dict(
        {
            "experiment": {"name": "proc", "participant": "P"},
            "streams": {"imu_left": {}, "rgb": {"fps": 10}, "eye_gaze": {}},
            "processors": processors,
            "storage": {"root": str(root), "image_format": "png", **storage},
            "mock": {"seed": 3, "jitter_ms": 0.0, "realtime": False},
        }
    )


def test_validate_processors_checks_parameters(tmp_path: Path) -> None:
    cfg = _graph_config(
        tmp_path,
        [
            {
                "id": "r",
                "type": "image.resize",
                "inputs": ["rgb"],
                "outputs": ["small"],
                "parameters": {"width": 10},
            }
        ],
    )
    with pytest.raises(ConfigError, match=r"processors\[r\].parameters \(image.resize\)") as ei:
        validate_processors(cfg)
    assert "width" in str(ei.value) and "accepted parameters" in str(ei.value)
    cfg = _graph_config(
        tmp_path,
        [
            {
                "id": "r",
                "type": "image.resize",
                "inputs": ["rgb"],
                "outputs": ["small"],
                "parameters": {"scale": 0.5, "bogus": 1},
            }
        ],
    )
    with pytest.raises(ConfigError, match="bogus"):
        validate_processors(cfg)
    cfg = _graph_config(
        tmp_path, [{"id": "r", "type": "no.such", "inputs": ["rgb"], "outputs": ["s"]}]
    )
    with pytest.raises(ConfigError, match=r"processors\[r\].type: unknown"):
        validate_processors(cfg)
    cfg = _graph_config(
        tmp_path,
        [
            {
                "id": "r",
                "type": "image.resize",
                "inputs": ["rgb"],
                "outputs": ["small"],
                "parameters": {"scale": 0.5},
            }
        ],
    )
    r = validate_processors(cfg)[0]
    assert r.parameters.scale == 0.5 and r.parameters_dict["interpolation"] == "area"
    assert r.describe()["class"] == "ariastreamlab.processing.builtin.image:Resize"


def test_graph_chains_processors_and_orders_them(
    tmp_path: Path, bus: EventBus, make_sample
) -> None:
    cfg = _graph_config(
        tmp_path,
        [
            {
                "id": "moving",
                "type": "imu.motion_detector",
                "inputs": ["lp"],
                "outputs": ["moving"],
                "parameters": {"window_ms": 10},
            },
            {
                "id": "lp",
                "type": "imu.lowpass",
                "inputs": ["imu_left"],
                "outputs": ["lp"],
                "parameters": {"alpha": 0.5},
            },
            {
                "id": "mag",
                "type": "imu.magnitude",
                "inputs": ["lp"],
                "outputs": ["mag"],
                "parameters": {"of": "gyro"},
            },
        ],
    )
    g = ProcessingGraph(cfg, bus, health_interval_s=0.05, lossless=True)
    assert g.order == ["lp", "mag", "moving"]
    out = bus.subscribe("derived.*", maxsize=10_000)
    health = bus.subscribe("health.pipeline", maxsize=100)
    g.start()
    for i in range(50):
        gyro = (0.0, 0.0, 1.0 if 10 <= i < 30 else 0.0)
        bus.publish(
            "raw.imu_left",
            make_sample(
                device_ns=i * 1_250_000,
                host_ns=i * 1_250_000,
                seq=i,
                data=ImuPayload((0, 0, 9.8), gyro),
            ),
        )
    _wait(lambda: g.stats()["moving"].outputs == 50)
    time.sleep(0.1)
    g.stop()
    by: dict[str, list[DerivedSample]] = {}
    for s in out.drain():
        by.setdefault(s.stream_id, []).append(s)
    assert {k: len(v) for k, v in by.items()} == {"lp": 50, "mag": 50, "moving": 50}
    assert all(isinstance(s.data, ImuPayload) for s in by["lp"])
    assert by["mag"][20].data == ScalarPayload(pytest.approx(1.0, abs=0.01), "rad/s")  # type: ignore[arg-type]
    assert by["mag"][20].source_streams == ("lp",)
    assert by["moving"][0].data.value == 0.0
    assert by["moving"][25].data.value == 1.0
    assert by["moving"][49].data.value == 0.0
    assert by["moving"][25].provenance.origin == "mock"
    h = health.drain()
    assert h and set(h[-1]) == {"lp", "mag", "moving"} and h[-1]["lp"]["fired"] == 50
    info = g.stream_info()
    assert info["lp"]["version"] == "1" and info["lp"]["parameters"]["alpha"] == 0.5
    assert info["moving"]["units"] == {"value": "moving (0/1)"}
    assert g.saved_outputs() == ["moving", "lp", "mag"]


def test_graph_disabled_processor_is_skipped(tmp_path: Path, bus: EventBus) -> None:
    cfg = _graph_config(
        tmp_path,
        [
            {
                "id": "r",
                "type": "image.resize",
                "inputs": ["rgb"],
                "outputs": ["s"],
                "enabled": False,
                "parameters": {"scale": 0.5},
            }
        ],
    )
    g = ProcessingGraph(cfg, bus)
    assert len(g) == 0 and g.describe() == []
    g.start()
    g.stop()


# --------------------------------------------------------------------------- built-ins


def _run_one(
    proc: Processor,
    params: dict[str, Any],
    samples: list[Sample],
    inputs: list[str],
    outputs: list[str],
    calibration=None,
):
    bus = EventBus()
    cfg = _cfg("p", "t", inputs, outputs, parameters=params)
    validated = proc.Parameters.model_validate(params) if proc.Parameters else params
    sub = bus.subscribe("derived.*", maxsize=10_000)
    w = ProcessorWorker(cfg, proc, _ctx(cfg, validated, calibration=calibration), bus)
    w.start()
    for s in samples:
        bus.publish(f"raw.{s.stream_id}", s)
    _wait(
        lambda: (
            w.stats().inputs_received.get(inputs[0], 0)
            == len([s for s in samples if s.stream_id == inputs[0]])
        )
    )
    w.stop()
    bus.close()
    return sub.drain(), w.stats()


def test_image_processors(provenance: Provenance) -> None:
    from ariastreamlab.processing.builtin.image import Crop, Grayscale, Resize

    img = np.random.default_rng(1).integers(0, 255, (40, 60, 3), dtype=np.uint8)
    frames = [Sample("d", "rgb", i, i, i, img, provenance) for i in range(2)]
    out, st = _run_one(Resize(), {"width": 30, "height": 20}, frames, ["rgb"], ["o"])
    assert out[0].data.shape == (20, 30, 3) and out[0].metadata["source_shape"] == [40, 60, 3]
    out, _ = _run_one(Resize(), {"scale": 0.5, "interpolation": "nearest"}, frames, ["rgb"], ["o"])
    assert out[0].data.shape == (20, 30, 3)
    out, _ = _run_one(Grayscale(), {}, frames, ["rgb"], ["o"])
    assert out[0].data.shape == (40, 60) and out[0].data.dtype == np.uint8
    out, _ = _run_one(Crop(), {"x": 10, "y": 5, "width": 20, "height": 10}, frames, ["rgb"], ["o"])
    assert out[0].data.shape == (10, 20, 3) and np.array_equal(out[0].data, img[5:15, 10:30])
    out, st = _run_one(
        Crop(), {"x": 100, "y": 5, "width": 20, "height": 10}, frames, ["rgb"], ["o"]
    )
    assert out == [] and st.errors == 2 and "outside" in (st.last_error or "")
    with pytest.raises(ValueError, match="scale"):
        Resize.Parameters(scale=0.5, width=3)
    with pytest.raises(ValueError, match="either"):
        Resize.Parameters(width=3)


def test_imu_lowpass_and_magnitude(provenance: Provenance) -> None:
    from ariastreamlab.processing.builtin.imu import Lowpass, Magnitude

    samples = [
        Sample(
            "d",
            "imu_left",
            i * 1_250_000,
            0,
            i,
            ImuPayload((0.0, 0.0, 10.0 if i else 0.0), (1.0, 0.0, 0.0)),
            provenance,
        )
        for i in range(400)
    ]
    out, _ = _run_one(Lowpass(), {"cutoff_hz": 5.0}, samples, ["imu_left"], ["o"])
    z = [s.data.accel_msec2[2] for s in out]
    assert z[0] == 0.0 and z[1] < 1.0 and z[-1] > 9.9  # settles at ~0.5 s with fc=5 Hz
    assert all(abs(s.data.gyro_radsec[0] - 1.0) < 1e-9 for s in out)
    out, _ = _run_one(Lowpass(), {"alpha": 1.0}, samples, ["imu_left"], ["o"])
    assert out[5].data.accel_msec2 == (0.0, 0.0, 10.0)
    out, _ = _run_one(
        Magnitude(), {"of": "accel", "remove_gravity": True}, samples[:3], ["imu_left"], ["o"]
    )
    assert out[1].data == ScalarPayload(pytest.approx(10 - 9.80665), "m/s^2")  # type: ignore[arg-type]
    out, _ = _run_one(Magnitude(), {"of": "gyro"}, samples[:1], ["imu_left"], ["o"])
    assert out[0].data == ScalarPayload(1.0, "rad/s")


def test_motion_detector_hysteresis_and_changes(provenance: Provenance) -> None:
    from ariastreamlab.processing.builtin.imu import MotionDetector

    def gyro(i: int) -> float:
        return 0.5 if 100 <= i < 200 else 0.0

    samples = [
        Sample(
            "d",
            "imu_left",
            i * 1_250_000,
            0,
            i,
            ImuPayload((0, 0, 9.8), (gyro(i), 0.0, 0.0)),
            provenance,
        )
        for i in range(300)
    ]
    out, _ = _run_one(
        MotionDetector(), {"threshold": 0.2, "window_ms": 20}, samples, ["imu_left"], ["o"]
    )
    values = [s.data.value for s in out]
    assert len(values) == 300
    assert values[50] == 0.0 and values[150] == 1.0 and values[290] == 0.0
    assert out[150].metadata["gyro_norm_radsec"] == pytest.approx(0.5, abs=0.03)
    out, _ = _run_one(
        MotionDetector(),
        {"threshold": 0.2, "window_ms": 20, "emit": "changes"},
        samples,
        ["imu_left"],
        ["o"],
    )
    assert [s.data.value for s in out] == [0.0, 1.0, 0.0]


def test_generic_processors(provenance: Provenance) -> None:
    from ariastreamlab.processing.builtin.generic import Downsample, Passthrough

    samples = [
        Sample("d", "imu_left", i * 1_250_000, 0, i, float(i), provenance) for i in range(80)
    ]
    out, _ = _run_one(Downsample(), {"every": 10}, samples, ["imu_left"], ["o"])
    assert [s.data for s in out] == [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0]
    out, _ = _run_one(Downsample(), {"rate_hz": 100}, samples, ["imu_left"], ["o"])
    assert len(out) == 10 and [s.data for s in out][:3] == [0.0, 8.0, 16.0]
    out, _ = _run_one(Passthrough(), {}, samples[:2], ["imu_left"], ["o"])
    assert out[1].data == 1.0 and out[1].stream_id == "o"
    with pytest.raises(ValueError, match="exactly one"):
        Downsample.Parameters(every=2, rate_hz=3)


# --------------------------------------------------------------------------- gaze


def test_fisheye_model_pinhole_limit_and_gaze_point() -> None:
    f, cx, cy = 500.0, 320.0, 240.0
    params = np.array([f, cx, cy] + [0.0] * 12)
    uv = fisheye624_project(np.array([0.0, 0.0, 1.0]), params)
    assert uv is not None and uv.tolist() == [cx, cy]
    uv = fisheye624_project(np.array([0.1, 0.0, 1.0]), params)
    assert uv is not None and abs(uv[0] - (cx + f * np.arctan(0.1))) < 1e-9  # equidistant fisheye
    assert fisheye624_project(np.array([0.0, 0.0, -1.0]), params) is None
    p = gaze_point_cpf(0.0, 0.0, 2.0)
    assert p.tolist() == [0.0, 0.0, 2.0]
    p = gaze_point_cpf(0.1, -0.2, 1.5)
    assert abs(np.linalg.norm(p) - 1.5) < 1e-12 and p[0] > 0 and p[1] < 0


def _fake_calibration() -> dict[str, Any]:
    # identity extrinsics, pinhole-like Fisheye624 with no distortion, 640x480
    eye = np.eye(4).tolist()
    return {
        "transform_device_cpf": eye,
        "cameras": {
            "camera-rgb": {
                "image_size": [640, 480],
                "projection_params": [500.0, 320.0, 240.0] + [0.0] * 12,
                "transform_device_camera": eye,
            }
        },
    }


def test_gaze_projection_with_and_without_frame(provenance: Provenance) -> None:
    from ariastreamlab.processing.builtin.gaze import ProjectToRgb

    gaze = GazePayload(0.0, 0.0, 1.0, True, 0, 0, 0, 0)
    gz = [Sample("d", "eye_gaze", 1_000_000 * i, 0, i, gaze, provenance) for i in range(3)]
    out, st = _run_one(ProjectToRgb(), {}, gz, ["eye_gaze"], ["g"], calibration=_fake_calibration())
    assert len(out) == 3 and out[0].data["x"] == 320.0 and out[0].data["y"] == 240.0
    assert out[0].data["in_image"] and out[0].data["valid"] and out[0].data["x_norm"] == 0.5
    # no calibration: nothing emitted, no error, one warning
    out, st = _run_one(ProjectToRgb(), {}, gz, ["eye_gaze"], ["g"])
    assert out == [] and st.errors == 0
    # with the frame as primary and gaze as secondary (nearest join), one output per frame
    img = np.zeros((480, 640, 3), np.uint8)
    frames = [Sample("d", "rgb", 1_000_000 * i + 300_000, 0, i, img, provenance) for i in range(2)]
    bus = EventBus()
    cfg = _cfg(
        "g", "t", ["rgb", "eye_gaze"], ["g"], execution={"join": "nearest", "tolerance_ms": 5}
    )
    sub = bus.subscribe("derived.g", maxsize=100)
    w = ProcessorWorker(
        cfg,
        ProjectToRgb(),
        _ctx(cfg, ProjectToRgb.Parameters(), calibration=_fake_calibration()),
        bus,
    )
    w.start()
    for s in gz:
        bus.publish("raw.eye_gaze", s)
    for s in frames:
        bus.publish("raw.rgb", s)
    _wait(lambda: w.stats().fired == 2)
    w.stop()
    items = sub.drain()
    assert len(items) == 2 and items[0].data["gaze_dt_ns"] == -300_000
    assert items[0].source_streams == ("rgb", "eye_gaze")
    # invalid gaze rows are tagged (and can be suppressed)
    bad = [
        Sample(
            "d",
            "eye_gaze",
            5,
            0,
            9,
            GazePayload(0.0, 0.0, float("nan"), False, 0, 0, 0, 0),
            provenance,
        )
    ]
    out, _ = _run_one(ProjectToRgb(), {}, bad, ["eye_gaze"], ["g"], calibration=_fake_calibration())
    assert out[0].data["valid"] is False and out[0].data["depth_m"] == 1.0
    out, _ = _run_one(
        ProjectToRgb(),
        {"emit_invalid": False},
        bad,
        ["eye_gaze"],
        ["g"],
        calibration=_fake_calibration(),
    )
    assert out == []


# --------------------------------------------------------------------------- experiment end-to-end


def test_experiment_runs_graph_and_stores_provenance(tmp_path: Path) -> None:
    from ariastreamlab import Experiment
    from ariastreamlab.storage.manifest import Manifest
    from ariastreamlab.storage.reader import SessionReader

    (tmp_path / "processors").mkdir()
    (tmp_path / "processors" / "norm.py").write_text(
        "from ariastreamlab.processing import Processor, Inputs, Emit\n"
        "from ariastreamlab.core.payloads import ScalarPayload\n"
        "class AccelNorm(Processor):\n"
        "    version = '7'\n"
        "    def process(self, inputs: Inputs):\n"
        "        a = inputs.primary.data.accel_msec2\n"
        "        return Emit('accel_norm', ScalarPayload((a[0]**2+a[1]**2+a[2]**2)**0.5, 'm/s^2'))\n"
    )
    cfg_path = tmp_path / "exp.json"
    cfg_path.write_text(
        json.dumps(
            {
                "experiment": {"name": "e2e", "participant": "P"},
                "streams": {"imu_left": {}, "rgb": {"fps": 10}},
                "processors": [
                    {
                        "id": "norm",
                        "type": "norm.py",
                        "inputs": ["imu_left"],
                        "outputs": ["accel_norm"],
                    },
                    {
                        "id": "small",
                        "type": "image.resize",
                        "inputs": ["rgb"],
                        "outputs": ["rgb_small"],
                        "parameters": {"scale": 0.5},
                        "save_output": False,
                    },
                    {
                        "id": "slow",
                        "type": "generic.downsample",
                        "inputs": ["accel_norm"],
                        "outputs": ["accel_norm_10hz"],
                        "parameters": {"rate_hz": 10},
                    },
                ],
                "storage": {"root": str(tmp_path / "sessions"), "image_format": "png"},
                "mock": {"seed": 1, "jitter_ms": 0.0, "realtime": False},
            }
        )
    )
    exp = Experiment.from_json(cfg_path, mock=True, duration_s=1.0)
    with exp:
        exp.run()
        pst = exp.processor_stats()
        root = exp.session_dir
    assert root is not None
    assert pst["norm"].fired == 800 and pst["norm"].outputs == 800 and pst["norm"].errors == 0
    assert pst["small"].outputs == 10
    assert 9 <= pst["slow"].outputs <= 11
    m = Manifest.load(root / "manifest.json")
    assert [p["id"] for p in m.processors] == ["norm", "small", "slow"]
    assert m.processors[0]["version"] == "7" and m.processors[0]["class"].endswith("norm:AccelNorm")
    assert m.processors[1]["parameters"] == {
        "width": None,
        "height": None,
        "scale": 0.5,
        "interpolation": "area",
    }
    assert m.pipeline["norm"]["fired"] == 800
    meta = json.loads((root / "derived" / "accel_norm" / "_meta.json").read_text())
    assert meta["producer"]["processor"] == "norm" and meta["producer"]["version"] == "7"
    assert meta["samples"] == 800
    assert not (root / "derived" / "rgb_small").exists()
    reader = SessionReader(root)
    rows = list(reader.iter_stream("accel_norm"))
    assert len(rows) == 800 and isinstance(rows[0], DerivedSample)
    assert rows[0].processor_id == "norm"
    assert rows[0].data.value == pytest.approx(9.8, abs=1.0)
    chained = list(reader.iter_stream("accel_norm_10hz"))
    assert 9 <= len(chained) <= 11
