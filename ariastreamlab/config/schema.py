"""JSON Schema export, with the registry's stream parameters spelled out."""

from __future__ import annotations

import json
from typing import Any

from ariastreamlab.config.models import ExperimentConfig
from ariastreamlab.core.registry import REGISTRY, ParameterSpec


def _param_schema(spec: ParameterSpec) -> dict[str, Any]:
    schema: dict[str, Any] = {"default": spec.default}
    if spec.description:
        schema["description"] = spec.description
    if spec.choices is not None:
        schema["enum"] = list(spec.choices)
    else:
        schema["type"] = "number"
        if spec.minimum is not None:
            schema["minimum"] = spec.minimum
        if spec.maximum is not None:
            schema["maximum"] = spec.maximum
    return schema


def export_json_schema() -> dict[str, Any]:
    """The schema of :class:`ExperimentConfig`, with one property per configurable stream key."""
    schema = ExperimentConfig.model_json_schema(by_alias=True)
    streams: dict[str, Any] = {}
    for key in REGISTRY.config_keys():
        desc = REGISTRY.get(REGISTRY.expand(key)[0])
        props: dict[str, Any] = {"enabled": {"type": "boolean", "default": True}}
        for name, spec in desc.parameters.items():
            props[name] = _param_schema(spec)
        streams[key] = {
            "type": "object",
            "description": desc.description
            if not REGISTRY.is_group(key)
            else f"{key}: {', '.join(REGISTRY.expand(key))}",
            "properties": props,
            "additionalProperties": False,
        }
    schema["properties"]["streams"] = {
        "type": "object",
        "properties": streams,
        "additionalProperties": False,
        "description": "Streams to enable; keys are stream ids or group aliases",
    }
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "AriaStreamLab experiment"
    return schema


def export_json_schema_text() -> str:
    """Pretty JSON text of :func:`export_json_schema`."""
    return json.dumps(export_json_schema(), indent=2)
