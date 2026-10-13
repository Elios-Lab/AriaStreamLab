"""Loading experiment JSON files with preset inheritance.

``"extends": "preset:rgb_imu"`` pulls ``ariastreamlab/config/presets/rgb_imu.json``;
``"extends": "../base.json"`` is resolved relative to the file being loaded. Inheritance is
single and recursive (a preset may itself extend another). Merge rule: dictionaries are
deep-merged key by key, every other value (lists included) is replaced by the child's.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ariastreamlab.config.models import ExperimentConfig, format_validation_error
from ariastreamlab.core.errors import ConfigError

PRESET_PREFIX = "preset:"
_MAX_DEPTH = 8


def preset_names() -> tuple[str, ...]:
    """Names of the bundled presets."""
    root = resources.files("ariastreamlab.config") / "presets"
    return tuple(
        sorted(p.name[: -len(".json")] for p in root.iterdir() if p.name.endswith(".json"))
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}",
            hint="JSON does not allow trailing commas or comments",
        ) from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: the top level must be a JSON object")
    return data


def _read_preset(name: str) -> dict[str, Any]:
    res = resources.files("ariastreamlab.config") / "presets" / f"{name}.json"
    if not res.is_file():
        raise ConfigError(
            f"unknown preset {name!r}", hint="available presets: " + ", ".join(preset_names())
        )
    data = json.loads(res.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``base`` updated by ``override``; nested dicts merge, everything else replaces."""
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_extends(data: dict[str, Any], base_dir: Path | None, _depth: int = 0) -> dict[str, Any]:
    """Recursively apply ``extends`` and return the merged dictionary (without the key)."""
    if _depth > _MAX_DEPTH:
        raise ConfigError("'extends' chain is too deep (cycle?)")
    parent_ref = data.get("extends")
    if parent_ref is None:
        return dict(data)
    if not isinstance(parent_ref, str):
        raise ConfigError("'extends' must be a string")
    if parent_ref.startswith(PRESET_PREFIX):
        parent = _read_preset(parent_ref[len(PRESET_PREFIX) :])
        parent_dir = None
    else:
        parent_path = Path(parent_ref)
        if not parent_path.is_absolute():
            parent_path = (base_dir or Path.cwd()) / parent_path
        parent = _read_json(parent_path)
        parent_dir = parent_path.parent
    parent = resolve_extends(parent, parent_dir, _depth + 1)
    child = {k: v for k, v in data.items() if k != "extends"}
    merged = deep_merge(parent, child)
    merged.pop("extends", None)
    return merged


def load_dict(
    data: dict[str, Any], *, base_dir: Path | None = None, source: Path | None = None
) -> ExperimentConfig:
    """Validate an already-parsed dictionary (resolving ``extends``)."""
    resolved = resolve_extends(data, base_dir)
    try:
        cfg = ExperimentConfig.model_validate(resolved)
    except ValidationError as exc:
        raise format_validation_error(exc, source) from None
    return _anchor_processors_path(cfg, base_dir)


def _anchor_processors_path(cfg: ExperimentConfig, base_dir: Path | None) -> ExperimentConfig:
    """Make ``processors_path`` relative to the JSON file; default to ``processors/`` next to it."""
    pp = cfg.processors_path
    if pp is not None:
        if not pp.is_absolute() and base_dir is not None:
            pp = (base_dir / pp).resolve()
        return cfg.model_copy(update={"processors_path": pp})
    if base_dir is not None and (base_dir / "processors").is_dir():
        return cfg.model_copy(update={"processors_path": (base_dir / "processors").resolve()})
    return cfg


def load_config(path: str | Path) -> ExperimentConfig:
    """Load and validate an experiment JSON file."""
    p = Path(path)
    return load_dict(_read_json(p), base_dir=p.parent, source=p)


def load_preset(name: str) -> ExperimentConfig:
    """Load a bundled preset as a full configuration (useful for ``--mock`` quick starts)."""
    return load_dict(_read_preset(name), base_dir=None, source=Path(f"preset:{name}"))


def dump_resolved(config: ExperimentConfig) -> str:
    """The fully resolved configuration as pretty JSON (what gets frozen into a session)."""
    return json.dumps(config.model_dump(mode="json", by_alias=True, exclude_none=True), indent=2)
