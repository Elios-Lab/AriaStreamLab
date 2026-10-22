"""The experiment designer: a JSON document edited through forms, validated live.

:class:`Designer` is the model behind the ``/design`` page and needs no NiceGUI: it holds
the experiment as a plain ``dict`` (exactly what ``experiment.json`` contains), knows which
streams and processors exist, validates the document with the same code path as
``ariastreamlab inspect-config`` and writes a compact file. The page in :mod:`.designer_ui`
only renders it. Everything the designer produces could have been typed by hand — the JSON
file stays the single source of truth and is what gets frozen into a session's manifest.
"""

from __future__ import annotations

import enum
import json
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from ariastreamlab.acquisition.base import StreamingPlan
from ariastreamlab.config.loader import load_dict, preset_names, resolve_extends
from ariastreamlab.config.models import ExecutionConfig, ExperimentConfig, ProcessorConfig
from ariastreamlab.core.errors import AriaStreamLabError
from ariastreamlab.core.registry import REGISTRY, ParameterSpec, StreamDescriptor

__all__ = ["Designer", "FieldSpec", "ProcessorEntry", "Report", "StreamEntry", "fields_of"]


# ----------------------------------------------------------------------------- field specs
@dataclass(frozen=True)
class FieldSpec:
    """One editable scalar of a Pydantic model, flattened for a form.

    Attributes:
        name: Field name (the JSON key).
        kind: ``bool`` | ``int`` | ``float`` | ``str`` | ``choice`` | ``list`` | ``model``.
        choices: Allowed values for ``choice``.
        optional: ``None`` is a legal value (``X | None``).
        default: Default value (JSON-compatible).
        description: Field description from the model.
        model: The nested model class for ``kind == "model"``.
        minimum, maximum: Numeric bounds when declared.
    """

    name: str
    kind: str
    choices: tuple[Any, ...] = ()
    optional: bool = False
    default: Any = None
    description: str = ""
    model: type[BaseModel] | None = None
    minimum: float | None = None
    maximum: float | None = None


def _strip_optional(ann: Any) -> tuple[Any, bool]:
    origin = typing.get_origin(ann)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
        return ann, True
    return ann, False


def _bounds(info: Any) -> tuple[float | None, float | None]:
    lo = hi = None
    for m in getattr(info, "metadata", ()):
        for attr, target in (("ge", "lo"), ("gt", "lo"), ("le", "hi"), ("lt", "hi")):
            v = getattr(m, attr, None)
            if v is not None:
                if target == "lo":
                    lo = float(v)
                else:
                    hi = float(v)
    return lo, hi


def fields_of(model: type[BaseModel]) -> list[FieldSpec]:
    """Flatten a Pydantic model's fields into :class:`FieldSpec` (nested models stay nested)."""
    out: list[FieldSpec] = []
    for name, info in model.model_fields.items():
        if name.startswith("schema"):
            continue  # $schema / schema_version are not for a form
        ann, optional = _strip_optional(info.annotation)
        origin = typing.get_origin(ann)
        default: Any = None
        if info.default_factory is not None:
            default = info.default_factory()  # type: ignore[call-arg]
        elif info.default is not None and repr(info.default) != "PydanticUndefined":
            default = info.default
        if isinstance(default, BaseModel):
            default = default.model_dump(mode="json")
        elif isinstance(default, enum.Enum):
            default = default.value
        elif isinstance(default, Path):
            default = str(default)
        lo, hi = _bounds(info)
        desc = info.description or ""
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out.append(FieldSpec(name, "model", default=default, description=desc, model=ann))
        elif origin is Literal:
            out.append(FieldSpec(name, "choice", typing.get_args(ann), optional, default, desc))
        elif isinstance(ann, type) and issubclass(ann, enum.Enum):
            out.append(
                FieldSpec(name, "choice", tuple(e.value for e in ann), optional, default, desc)
            )
        elif ann is bool:
            out.append(FieldSpec(name, "bool", (), optional, default, desc))
        elif ann is int:
            out.append(FieldSpec(name, "int", (), optional, default, desc, None, lo, hi))
        elif ann is float:
            out.append(FieldSpec(name, "float", (), optional, default, desc, None, lo, hi))
        elif origin in (list, tuple) or ann in (list, tuple):
            out.append(FieldSpec(name, "list", (), optional, default or [], desc))
        elif origin is dict or ann is dict:
            continue  # streams / parameters dicts are edited by dedicated widgets
        else:  # str, Path and anything else textual
            out.append(FieldSpec(name, "str", (), optional, default, desc))
    return out


# ----------------------------------------------------------------------------- catalogue
@dataclass(frozen=True)
class StreamEntry:
    """A key a user may write under ``streams`` and what it controls."""

    key: str
    members: tuple[str, ...]
    descriptor: StreamDescriptor
    parameters: tuple[ParameterSpec, ...]

    @property
    def live(self) -> bool:  # noqa: D102
        return self.descriptor.live_available

    @property
    def is_image(self) -> bool:  # noqa: D102
        return self.descriptor.is_image


@dataclass(frozen=True)
class ProcessorEntry:
    """A processor type the designer can add."""

    type: str
    origin: str
    version: str
    description: str
    parameters: tuple[FieldSpec, ...]
    output_payloads: Mapping[str, str]

    @property
    def default_parameters(self) -> dict[str, Any]:  # noqa: D102
        return {f.name: f.default for f in self.parameters}


@dataclass
class Report:
    """Result of :meth:`Designer.validate`."""

    config: ExperimentConfig | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    plan: StreamingPlan | None = None

    @property
    def ok(self) -> bool:  # noqa: D102
        return self.config is not None and not self.errors


# ----------------------------------------------------------------------------- the model
class Designer:
    """An experiment document under construction.

    Args:
        doc: The document (a parsed ``experiment.json``); ``extends`` is resolved at once so
            every section is editable.
        base_dir: Where relative paths (``processors_path``, the save location) are anchored;
            default: the current directory.
    """

    def __init__(self, doc: Mapping[str, Any] | None = None, *, base_dir: Path | None = None):
        self.base_dir = (base_dir or Path.cwd()).resolve()
        data = dict(doc or {"extends": "preset:base", "experiment": {"name": "my_experiment"}})
        self.preset = str(data.get("extends") or "")
        self.doc: dict[str, Any] = self._full(resolve_extends(data, self.base_dir))
        self._catalogue: list[ProcessorEntry] | None = None

    # -- construction ------------------------------------------------------------------
    @classmethod
    def from_preset(cls, name: str, *, base_dir: Path | None = None) -> Designer:
        """Start from a bundled preset (``"base"``, ``"rgb_imu"``…)."""
        return cls({"extends": f"preset:{name}", "experiment": {"name": name}}, base_dir=base_dir)

    @classmethod
    def from_file(cls, path: str | Path) -> Designer:
        """Load an existing experiment file."""
        p = Path(path).resolve()
        return cls(json.loads(p.read_text(encoding="utf-8")), base_dir=p.parent)

    @staticmethod
    def presets() -> tuple[str, ...]:  # noqa: D102
        return preset_names()

    @staticmethod
    def _full(resolved: dict[str, Any]) -> dict[str, Any]:
        """Every section present with defaults filled in, so forms have a value to show."""
        data = dict(resolved)
        data.pop("extends", None)
        data.pop("$schema", None)
        try:
            cfg = ExperimentConfig.model_validate(data)
            full = cfg.model_dump(mode="json", by_alias=True)
        except Exception:
            # invalid as it stands (e.g. a bad processor): keep the user's sections and fill
            # only the missing ones from a default model
            base = ExperimentConfig.model_validate({"experiment": {"name": "x"}}).model_dump(
                mode="json", by_alias=True
            )
            full = {**base, **data}
        full.pop("$schema", None)
        full["streams"] = {k: dict(v) for k, v in (data.get("streams") or {}).items()}
        full["processors"] = [_full_processor(p) for p in data.get("processors") or []]
        if full.get("processors_path") is None:
            full.pop("processors_path", None)
        return full

    # -- streams -----------------------------------------------------------------------
    @staticmethod
    def stream_entries() -> list[StreamEntry]:
        """Every configurable stream key, in registry order."""
        out = []
        for key in REGISTRY.config_keys():
            members = REGISTRY.expand(key)
            d = REGISTRY.get(members[0])
            out.append(StreamEntry(key, members, d, tuple(d.parameters.values())))
        return out

    def stream_enabled(self, key: str) -> bool:  # noqa: D102
        cfg = self.doc["streams"].get(key)
        return bool(cfg is not None and cfg.get("enabled", True))

    def set_stream_enabled(self, key: str, enabled: bool) -> None:
        """Enable a stream (keeping any parameters already set) or remove it."""
        streams: dict[str, Any] = self.doc["streams"]
        if enabled:
            cfg = streams.setdefault(key, {})
            cfg.pop("enabled", None)
        else:
            streams.pop(key, None)

    def stream_param(self, key: str, name: str) -> Any:  # noqa: D102
        spec = REGISTRY.get(REGISTRY.expand(key)[0]).parameters[name]
        return self.doc["streams"].get(key, {}).get(name, spec.default)

    def set_stream_param(self, key: str, name: str, value: Any) -> None:  # noqa: D102
        cfg = self.doc["streams"].setdefault(key, {})
        spec = REGISTRY.get(REGISTRY.expand(key)[0]).parameters[name]
        if value is None or value == spec.default:
            cfg.pop(name, None)
        else:
            cfg[name] = value

    # -- processors --------------------------------------------------------------------
    def processors_path(self) -> Path | None:
        """Where file processors are looked up (``processors_path`` or ``processors/``)."""
        pp = self.doc.get("processors_path")
        if pp:
            p = Path(pp)
            return p if p.is_absolute() else (self.base_dir / p).resolve()
        default = self.base_dir / "processors"
        return default if default.is_dir() else None

    def catalogue(self, refresh: bool = False) -> list[ProcessorEntry]:
        """Processor types that can be added (built-ins, entry points, files)."""
        if self._catalogue is None or refresh:
            from ariastreamlab.processing.plugins import list_processors

            out = []
            for info in list_processors(self.processors_path()):
                params = fields_of(info.cls.Parameters) if info.cls.Parameters else []
                payloads = {k: v.payload for k, v in info.cls.outputs.items()}
                out.append(
                    ProcessorEntry(
                        info.name,
                        info.origin,
                        info.version,
                        info.description,
                        tuple(params),
                        payloads,
                    )
                )
            self._catalogue = out
        return self._catalogue

    def entry_for(self, type_name: str) -> ProcessorEntry | None:
        """The catalogue entry a configuration ``type`` resolves to (``None`` if unknown)."""
        for e in self.catalogue():
            if e.type == type_name:
                return e
        # a bare file name ("blink_counter.py") is listed as "blink_counter.py:Class"
        same_file = [e for e in self.catalogue() if e.type.split(":")[0] == type_name]
        return same_file[0] if len(same_file) == 1 else None

    def add_processor(self, type_name: str) -> int:
        """Append a processor of ``type_name`` with a unique id and default parameters."""
        base = type_name.split(".")[-1].split(":")[-1].removesuffix(".py").replace("-", "_")
        ids = {p["id"] for p in self.doc["processors"]}
        pid, n = base, 2
        while pid in ids or pid in REGISTRY:
            pid, n = f"{base}_{n}", n + 1
        entry = self.entry_for(type_name)
        self.doc["processors"].append(
            _full_processor(
                {
                    "id": pid,
                    "type": type_name,
                    "inputs": self.input_options(len(self.doc["processors"]))[:1],
                    "outputs": [pid],
                    "parameters": entry.default_parameters if entry else {},
                }
            )
        )
        return len(self.doc["processors"]) - 1

    def remove_processor(self, index: int) -> None:  # noqa: D102
        del self.doc["processors"][index]

    def move_processor(self, index: int, delta: int) -> int:
        """Move a processor up/down (order is cosmetic; the graph is topological)."""
        procs = self.doc["processors"]
        j = max(0, min(len(procs) - 1, index + delta))
        procs.insert(j, procs.pop(index))
        return j

    def input_options(self, index: int) -> list[str]:
        """Streams a processor at ``index`` may read: enabled raw streams + other outputs."""
        raw: list[str] = []
        for key, cfg in self.doc["streams"].items():
            if not cfg.get("enabled", True):
                continue
            try:
                raw.extend(REGISTRY.expand(key))
            except Exception:
                continue
        outs = [
            o
            for i, p in enumerate(self.doc["processors"])
            if i != index and p.get("enabled", True)
            for o in p.get("outputs", [])
        ]
        return [*raw, *outs]

    def all_stream_ids(self) -> list[str]:
        """Enabled raw streams followed by every processor output (for ``dashboard.show``)."""
        return self.input_options(-1)

    # -- validation and output ---------------------------------------------------------
    def validate(self) -> Report:
        """Validate like ``inspect-config``: schema, registry, processors, live plan."""
        rep = Report()
        try:
            cfg = load_dict(json.loads(self.to_json(compact=False)), base_dir=self.base_dir)
        except AriaStreamLabError as exc:
            rep.errors = _error_lines(str(exc))
            return rep
        except Exception as exc:  # pragma: no cover - defensive
            rep.errors = [str(exc)]
            return rep
        rep.config = cfg
        if cfg.processors:
            from ariastreamlab.processing.graph import validate_processors

            try:
                validate_processors(cfg, self.processors_path())
            except AriaStreamLabError as exc:
                rep.errors = _error_lines(str(exc))
                return rep
        try:
            from ariastreamlab.acquisition.aria_gen2.profiles import build_plan

            rep.plan = build_plan(cfg, None)
            rep.warnings = list(rep.plan.warnings)
        except AriaStreamLabError as exc:
            rep.warnings = [str(exc)]
        if not cfg.enabled_streams():
            rep.warnings.insert(0, "no stream is enabled")
        return rep

    def to_dict(self, compact: bool = True) -> dict[str, Any]:
        """The document; ``compact`` drops values equal to the defaults (what a student writes)."""
        doc = json.loads(json.dumps(self.doc, default=str))
        if not compact:
            return dict(doc)
        try:
            cfg = ExperimentConfig.model_validate(
                {k: v for k, v in doc.items() if k != "processors_path"}
            )
        except Exception:
            return self._strip_default_parameters(self._compact_invalid(doc))
        out = cfg.model_dump(mode="json", by_alias=True, exclude_defaults=True, exclude_none=True)
        out.pop("$schema", None)
        out = {"schema_version": cfg.schema_version, **out}
        out["experiment"] = {"name": cfg.experiment.name, **out.get("experiment", {})}
        out["streams"] = {k: _compact_stream(k, v) for k, v in doc.get("streams", {}).items()}
        out["processors"] = [
            _compact_processor(p) for p in cfg.processors
        ]  # keeps the user's order
        if doc.get("processors_path"):
            out["processors_path"] = doc["processors_path"]
        if not out["processors"]:
            out.pop("processors")
        return self._strip_default_parameters(out)

    def _strip_default_parameters(self, out: dict[str, Any]) -> dict[str, Any]:
        """Drop processor parameters equal to the processor's own defaults."""
        for p in out.get("processors", []):
            entry = self.entry_for(str(p.get("type", "")))
            params = p.get("parameters")
            if entry is None or not isinstance(params, dict):
                continue
            defaults = entry.default_parameters
            kept = {k: v for k, v in params.items() if k not in defaults or v != defaults[k]}
            if kept:
                p["parameters"] = kept
            else:
                p.pop("parameters", None)
        return out

    @staticmethod
    def _compact_invalid(doc: dict[str, Any]) -> dict[str, Any]:
        """Best-effort compaction while the document does not validate (preview only)."""
        defaults = ExperimentConfig.model_validate({"experiment": {"name": "x"}}).model_dump(
            mode="json", by_alias=True
        )
        out: dict[str, Any] = {"schema_version": doc.get("schema_version", 1)}
        for key, value in doc.items():
            if key in ("schema_version", "$schema", "extends"):
                continue
            if key == "streams":
                out[key] = {k: _compact_stream(k, v) for k, v in value.items()}
            elif key == "processors":
                out[key] = [_compact_processor_dict(p) for p in value]
            elif isinstance(value, dict) and isinstance(defaults.get(key), dict):
                stripped = _strip(value, defaults[key])
                if key == "experiment":
                    stripped = {"name": value.get("name", ""), **stripped}
                if stripped or key == "experiment":
                    out[key] = stripped
            elif value is not None and value != defaults.get(key):
                out[key] = value
        return out

    def to_json(self, compact: bool = True) -> str:  # noqa: D102
        return json.dumps(self.to_dict(compact), indent=2, ensure_ascii=False)

    def save(self, path: str | Path) -> Path:
        """Write the compact document to ``path`` (relative to ``base_dir``)."""
        p = Path(path)
        if not p.is_absolute():
            p = self.base_dir / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(compact=True) + "\n", encoding="utf-8")
        return p


# ----------------------------------------------------------------------------- helpers
def _full_processor(p: Mapping[str, Any]) -> dict[str, Any]:
    """A processor entry with every key present (defaults filled in)."""
    out = {
        "id": p.get("id", ""),
        "type": p.get("type", ""),
        "inputs": list(p.get("inputs", [])),
        "outputs": list(p.get("outputs", [])),
        "enabled": bool(p.get("enabled", True)),
        "parameters": dict(p.get("parameters", {})),
        "execution": {
            **ExecutionConfig().model_dump(mode="json"),
            **dict(p.get("execution", {})),
        },
        "save_output": bool(p.get("save_output", True)),
    }
    if "drop_policy" in out["execution"] and hasattr(out["execution"]["drop_policy"], "value"):
        out["execution"]["drop_policy"] = out["execution"]["drop_policy"].value
    return out


def _strip(value: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Drop entries equal to their defaults (recursively for nested dicts)."""
    out: dict[str, Any] = {}
    for k, v in value.items():
        d = defaults.get(k)
        if isinstance(v, dict) and isinstance(d, dict):
            sub = _strip(v, d)
            if sub:
                out[k] = sub
        elif v is not None and v != d:
            out[k] = v
    return out


def _compact_stream(key: str, cfg: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in cfg.items() if k != "enabled" or v is False}
    try:
        specs = REGISTRY.get(REGISTRY.expand(key)[0]).parameters
    except Exception:
        return out
    return {k: v for k, v in out.items() if k not in specs or v != specs[k].default}


def _compact_processor_dict(p: Mapping[str, Any]) -> dict[str, Any]:
    """Compact a (possibly invalid) processor entry for the preview."""
    out = {k: v for k, v in p.items() if k not in ("execution", "enabled", "save_output")}
    execution = _strip(p.get("execution", {}), ExecutionConfig().model_dump(mode="json"))
    if execution:
        out["execution"] = execution
    if not p.get("enabled", True):
        out["enabled"] = False
    if not p.get("save_output", True):
        out["save_output"] = False
    return out


def _compact_processor(p: ProcessorConfig) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": p.id,
        "type": p.type,
        "inputs": list(p.inputs),
        "outputs": list(p.outputs),
    }
    if not p.enabled:
        out["enabled"] = False
    if p.parameters:
        out["parameters"] = dict(p.parameters)
    execution = p.execution.model_dump(mode="json", exclude_defaults=True)
    if execution:
        out["execution"] = execution
    if not p.save_output:
        out["save_output"] = False
    return out


def _error_lines(text: str) -> list[str]:
    lines = [ln.strip().lstrip("- ").strip().removeprefix("<root>: ") for ln in text.splitlines()]
    body = [ln for ln in lines[1:] if ln and not ln.startswith("hint:")] if len(lines) > 1 else []
    return body or [lines[0]]
