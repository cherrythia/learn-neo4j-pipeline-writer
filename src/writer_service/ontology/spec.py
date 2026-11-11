"""Plain, database-independent description of an ontology version."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Namespace = Literal["core", "ext"]
Datatype = Literal["string", "int", "float", "date", "bool"]
Cardinality = Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]


@dataclass(frozen=True)
class PropertySpec:
    name: str
    datatype: Datatype = "string"
    required: bool = False
    description: str | None = None


@dataclass(frozen=True)
class ClassSpec:
    name: str
    identity_key: str
    properties: tuple[PropertySpec, ...]
    parent: str | None = None  # taxonomy: subclass of
    namespace: Namespace = "core"
    description: str | None = None
    deprecated: bool = False


@dataclass(frozen=True)
class RelationSpec:
    name: str
    domain: str
    range: str
    cardinality: Cardinality = "many_to_many"
    namespace: Namespace = "core"
    description: str | None = None
    deprecated: bool = False


@dataclass(frozen=True)
class OntologySpec:
    classes: tuple[ClassSpec, ...]
    relations: tuple[RelationSpec, ...]

    def active_classes(self) -> tuple[ClassSpec, ...]:
        return tuple(c for c in self.classes if not c.deprecated)
