"""Processor discovery: built-ins, entry points and files next to the experiment JSON.

A ``type`` in the configuration is resolved in this order:

1. a built-in name such as ``"image.resize"`` (:data:`BUILTINS`);
2. a name registered by an installed package under the ``ariastreamlab.processors``
   entry-point group (``[project.entry-points."ariastreamlab.processors"]`` in its
   ``pyproject.toml``);
3. ``"package.module:ClassName"`` — any importable class;
4. ``"my_file.py:ClassName"`` or ``"my_file:ClassName"`` or just ``"my_file"`` (when the file
   defines exactly one :class:`Processor` subclass), looked up in ``processors_path`` from the
   configuration (default: the ``processors/`` directory next to the experiment JSON).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from ariastreamlab.core.errors import ProcessingError
from ariastreamlab.processing.base import Processor

__all__ = ["BUILTINS", "ENTRY_POINT_GROUP", "ProcessorInfo", "list_processors", "resolve"]

ENTRY_POINT_GROUP = "ariastreamlab.processors"

#: Built-in processors: config ``type`` → ``"module:Class"``.
BUILTINS: dict[str, str] = {
    "image.resize": "ariastreamlab.processing.builtin.image:Resize",
    "image.grayscale": "ariastreamlab.processing.builtin.image:Grayscale",
    "image.crop": "ariastreamlab.processing.builtin.image:Crop",
    "imu.lowpass": "ariastreamlab.processing.builtin.imu:Lowpass",
    "imu.magnitude": "ariastreamlab.processing.builtin.imu:Magnitude",
    "imu.motion_detector": "ariastreamlab.processing.builtin.imu:MotionDetector",
    "gaze.project_to_rgb": "ariastreamlab.processing.builtin.gaze:ProjectToRgb",
    "generic.downsample": "ariastreamlab.processing.builtin.generic:Downsample",
    "generic.passthrough": "ariastreamlab.processing.builtin.generic:Passthrough",
}


@dataclass(frozen=True)
class ProcessorInfo:
    """A discoverable processor (for ``ariastreamlab processors``)."""

    name: str
    origin: str  # builtin | entry_point | file
    cls: type[Processor]

    @property
    def description(self) -> str:  # noqa: D102
        if self.cls.description:
            return self.cls.description
        doc = inspect.getdoc(self.cls) or ""
        return doc.splitlines()[0] if doc else ""

    @property
    def version(self) -> str:  # noqa: D102
        return self.cls.version

    @property
    def qualified(self) -> str:  # noqa: D102
        return f"{self.cls.__module__}:{self.cls.__qualname__}"


def resolve(type_name: str, processors_path: Path | None = None) -> type[Processor]:
    """Turn a configuration ``type`` into a :class:`Processor` subclass."""
    if type_name in BUILTINS:
        return _import_class(BUILTINS[type_name], type_name)
    ep = _entry_points().get(type_name)
    if ep is not None:
        cls = ep.load()
        return _check(cls, type_name)
    if ":" in type_name:
        mod, _, cls_name = type_name.partition(":")
        if mod.endswith(".py") or (processors_path and (processors_path / f"{mod}.py").exists()):
            return _from_file(mod, cls_name, processors_path, type_name)
        try:
            return _import_class(type_name, type_name)
        except ProcessingError:
            if processors_path is not None:
                return _from_file(mod, cls_name, processors_path, type_name)
            raise
    # bare file name
    if processors_path is not None or type_name.endswith(".py"):
        return _from_file(type_name, None, processors_path, type_name)
    known = ", ".join(sorted(BUILTINS))
    raise ProcessingError(
        f"unknown processor type {type_name!r}",
        hint=f"built-ins: {known}; or use 'package.module:Class' / 'file.py:Class'",
    )


def list_processors(processors_path: Path | None = None) -> list[ProcessorInfo]:
    """Everything :func:`resolve` would find (files only when ``processors_path`` is given)."""
    out: list[ProcessorInfo] = []
    for name, target in BUILTINS.items():
        out.append(ProcessorInfo(name, "builtin", _import_class(target, name)))
    for name, ep in _entry_points().items():
        try:
            out.append(ProcessorInfo(name, "entry_point", _check(ep.load(), name)))
        except Exception:
            continue
    if processors_path is not None and processors_path.is_dir():
        for py in sorted(processors_path.glob("*.py")):
            try:
                module = _load_file(py)
            except Exception:
                continue
            for cls_name, cls in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(cls, Processor)
                    and cls is not Processor
                    and not inspect.isabstract(cls)
                ):
                    out.append(ProcessorInfo(f"{py.name}:{cls_name}", "file", cls))
    return out


# -- helpers ---------------------------------------------------------------------------------
def _entry_points() -> dict[str, importlib.metadata.EntryPoint]:
    eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    return {ep.name: ep for ep in eps}


def _import_class(target: str, type_name: str) -> type[Processor]:
    mod_name, _, cls_name = target.partition(":")
    if not mod_name or not cls_name:
        raise ProcessingError(f"processor {type_name!r}: expected 'module:Class', got {target!r}")
    try:
        module = importlib.import_module(mod_name)
    except ImportError as exc:
        raise ProcessingError(
            f"processor {type_name!r}: cannot import module {mod_name!r}: {exc}",
            hint="is the package installed in this environment?",
        ) from exc
    cls = getattr(module, cls_name, None)
    if cls is None:
        raise ProcessingError(f"processor {type_name!r}: {mod_name} has no class {cls_name!r}")
    return _check(cls, type_name)


def _check(cls: object, type_name: str) -> type[Processor]:
    if not (inspect.isclass(cls) and issubclass(cls, Processor)):
        raise ProcessingError(
            f"processor {type_name!r} resolves to {cls!r}, which is not a Processor subclass",
            hint="subclass ariastreamlab.processing.Processor and implement process()",
        )
    if inspect.isabstract(cls):
        raise ProcessingError(
            f"processor {type_name!r} ({cls.__name__}) does not implement process()"
        )
    return cls


def _from_file(
    stem: str, cls_name: str | None, processors_path: Path | None, type_name: str
) -> type[Processor]:
    name = stem[:-3] if stem.endswith(".py") else stem
    candidates = []
    if processors_path is not None:
        candidates.append(processors_path / f"{name}.py")
    candidates.append(Path(f"{name}.py"))
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        where = f" in {processors_path}" if processors_path else ""
        raise ProcessingError(
            f"processor {type_name!r}: file {name}.py not found{where}",
            hint="set 'processors_path' in the configuration or use 'package.module:Class'",
        )
    module = _load_file(path)
    if cls_name:
        cls = getattr(module, cls_name, None)
        if cls is None:
            raise ProcessingError(f"processor {type_name!r}: {path} has no class {cls_name!r}")
        return _check(cls, type_name)
    found = [
        c
        for _, c in inspect.getmembers(module, inspect.isclass)
        if issubclass(c, Processor)
        and c is not Processor
        and not inspect.isabstract(c)
        and c.__module__ == module.__name__
    ]
    if len(found) != 1:
        raise ProcessingError(
            f"processor {type_name!r}: {path} defines {len(found)} Processor classes",
            hint="name the class: 'file.py:ClassName'",
        )
    return found[0]


def _load_file(path: Path) -> ModuleType:
    mod_name = f"ariastreamlab_user_processors.{path.stem}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ProcessingError(f"cannot load processor file {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[mod_name]
        raise ProcessingError(f"error while importing {path}: {type(exc).__name__}: {exc}") from exc
    return module
