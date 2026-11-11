"""JSON schemas for structured output, derived from pydantic models."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic's JSON schema, closed for structured output.

    Every object gets ``additionalProperties: false`` and lists all its properties as
    required (structured-output APIs need this; optional fields stay nullable/defaulted on
    the pydantic side, which still validates the result).
    """
    schema = copy.deepcopy(model.model_json_schema())

    def close(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = sorted(node["properties"])
            for value in node.values():
                close(value)
        elif isinstance(node, list):
            for value in node:
                close(value)

    close(schema)
    return schema
