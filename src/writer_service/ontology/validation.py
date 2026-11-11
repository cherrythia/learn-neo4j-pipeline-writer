"""The publish gate: an ontology version must pass these checks before it is published.

Names are interpolated into Cypher (labels, relationship types, property keys), so they are
restricted to safe identifier patterns here; this is what keeps generated Cypher injection-free.
"""

from __future__ import annotations

import re

from writer_service.ontology.spec import OntologySpec

CLASS_NAME = re.compile(r"^[A-Z][A-Za-z0-9]{0,63}$")
RELATION_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
PROPERTY_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def validate(spec: OntologySpec, previous: OntologySpec | None = None) -> list[str]:
    errors: list[str] = []
    names = [c.name for c in spec.classes]
    by_name = {c.name: c for c in spec.classes}

    for name in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"duplicate class '{name}' (class names are unique across namespaces)")

    for c in spec.classes:
        if not CLASS_NAME.match(c.name):
            errors.append(f"invalid class name {c.name!r} (use CamelCase letters and digits)")
        props = {p.name: p for p in c.properties}
        for p in c.properties:
            if not PROPERTY_NAME.match(p.name):
                errors.append(f"class '{c.name}': invalid property name {p.name!r}")
        key = props.get(c.identity_key)
        if key is None:
            errors.append(
                f"class '{c.name}': identity key '{c.identity_key}' is not one of its properties"
            )
        elif not key.required:
            errors.append(f"class '{c.name}': identity key '{c.identity_key}' must be required")
        if c.parent is not None and c.parent not in by_name:
            errors.append(f"class '{c.name}': unknown parent '{c.parent}'")

    for c in spec.classes:  # taxonomy must be a forest
        seen: set[str] = set()
        node = c
        while node.parent is not None and node.parent in by_name:
            if node.name in seen:
                errors.append(f"taxonomy cycle involving '{c.name}'")
                break
            seen.add(node.name)
            node = by_name[node.parent]

    rel_names = [r.name for r in spec.relations]
    for name in sorted({n for n in rel_names if rel_names.count(n) > 1}):
        errors.append(f"duplicate relation '{name}'")
    for r in spec.relations:
        if not RELATION_NAME.match(r.name):
            errors.append(f"invalid relation name {r.name!r} (use UPPER_SNAKE_CASE)")
        if r.deprecated:
            continue
        for end, cls_name in (("domain", r.domain), ("range", r.range)):
            target = by_name.get(cls_name)
            if target is None:
                errors.append(f"relation '{r.name}': unknown {end} '{cls_name}'")
            elif target.deprecated:
                errors.append(f"relation '{r.name}': {end} class '{cls_name}' is deprecated")

    if previous is not None:  # deprecate, never delete
        for old in previous.classes:
            if old.name not in by_name:
                errors.append(
                    f"class '{old.name}' was removed from the published ontology; "
                    "deprecate it instead"
                )
        current_rels = set(rel_names)
        for old_rel in previous.relations:
            if old_rel.name not in current_rels:
                errors.append(
                    f"relation '{old_rel.name}' was removed from the published ontology; "
                    "deprecate it instead"
                )
    return errors
