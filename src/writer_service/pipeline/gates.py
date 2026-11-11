"""Hard gates (ADR-0004): a candidate that fails one is never scored or written.

* **Schema gate** – does the candidate fit the published ontology? Values are coerced to the
  declared datatypes; failure means *reject*.
* **Constraint gate** – does it contradict an accepted fact in the graph, or an earlier
  candidate in the same batch? Contradictions are *conflicts*: they go to human review and are
  never auto-overwritten.

Disjointness is structural here: nodes are MERGEd per class label on the class's identity key,
so one node can never become two unrelated classes; the gate checks what can still contradict
(property values and relation cardinality).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from writer_service.ontology.neo4j_schema import neo4j_label
from writer_service.ontology.spec import ClassSpec, OntologySpec, RelationSpec
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    EntityRef,
    Scalar,
    canonical_text,
    match_key,
)
from writer_service.pipeline.graph_view import GraphView

AnyCandidate = CandidateEntity | CandidateRelation
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRUE, _FALSE = {"true", "yes", "y", "1"}, {"false", "no", "n", "0"}


class _CoercionError(ValueError):
    pass


def coerce(value: Scalar, datatype: str) -> Scalar:
    """Convert ``value`` to ``datatype`` or raise. ``None`` stays ``None``."""
    if value is None:
        return None
    if datatype == "string":
        return canonical_text(str(value))
    if datatype == "int":
        if isinstance(value, bool | float | date):
            raise _CoercionError
        if isinstance(value, int):
            return value
        text = canonical_text(str(value))
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        raise _CoercionError
    if datatype == "float":
        if isinstance(value, bool | date):
            raise _CoercionError
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise _CoercionError from exc
    if datatype == "date":
        if isinstance(value, date):
            return value
        text = canonical_text(str(value))
        if _ISO_DATE.match(text):
            try:
                return date.fromisoformat(text)
            except ValueError as exc:
                raise _CoercionError from exc
        raise _CoercionError
    if datatype == "bool":
        if isinstance(value, bool):
            return value
        text = canonical_text(str(value)).casefold()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise _CoercionError
    raise _CoercionError


def _ancestors(spec: OntologySpec, name: str) -> list[str]:
    by_name = {c.name: c for c in spec.classes}
    chain, node = [], by_name.get(name)
    while node is not None and node.name not in chain:
        chain.append(node.name)
        node = by_name.get(node.parent) if node.parent else None
    return chain


def _is_a(spec: OntologySpec, child: str, parent: str) -> bool:
    return parent in _ancestors(spec, child)


is_a = _is_a  # public: ``child`` is ``parent`` or one of its subclasses


def _class(spec: OntologySpec, name: str, failures: list[str]) -> ClassSpec | None:
    cls = next((c for c in spec.classes if c.name == name), None)
    if cls is None:
        failures.append(f"schema: unknown class '{name}'")
    elif cls.deprecated:
        failures.append(f"schema: class '{name}' is deprecated")
    return cls


def schema_gate(candidate: AnyCandidate, spec: OntologySpec) -> tuple[list[str], AnyCandidate]:
    """Return (failures, normalised candidate). Failures are prefixed ``schema:``."""
    failures: list[str] = []
    if isinstance(candidate, CandidateEntity):
        cls = _class(spec, candidate.class_name, failures)
        if cls is None or cls.deprecated:
            return failures, candidate
        declared = {p.name: p for p in cls.properties}
        out: dict[str, Scalar] = {}
        for name, value in candidate.properties.items():
            prop = declared.get(name)
            if prop is None:
                failures.append(f"schema: class '{cls.name}' has no property '{name}'")
                continue
            try:
                out[name] = coerce(value, prop.datatype)
            except _CoercionError:
                failures.append(f"schema: property '{name}' expects {prop.datatype}, got {value!r}")
        for prop in cls.properties:
            if prop.required and (out.get(prop.name) in (None, "")):
                failures.append(f"schema: required property '{prop.name}' is missing")
        return failures, candidate.model_copy(update={"properties": out})

    rel = next((r for r in spec.relations if r.name == candidate.relation), None)
    if rel is None:
        return [f"schema: unknown relation '{candidate.relation}'"], candidate
    if rel.deprecated:
        return [f"schema: relation '{rel.name}' is deprecated"], candidate
    src = _class(spec, candidate.source.class_name, failures)
    tgt = _class(spec, candidate.target.class_name, failures)
    if failures:
        return failures, candidate
    assert src is not None and tgt is not None
    if not (_is_a(spec, src.name, rel.domain) and _is_a(spec, tgt.name, rel.range)):
        failures.append(
            f"schema: {rel.name} expects {rel.domain} -> {rel.range}, got {src.name} -> {tgt.name}"
        )
    source = EntityRef(class_name=src.name, key=canonical_text(candidate.source.key))
    target = EntityRef(class_name=tgt.name, key=canonical_text(candidate.target.key))
    if not source.key:
        failures.append("schema: source key is empty")
    if not target.key:
        failures.append("schema: target key is empty")
    if candidate.properties:
        failures.append(
            f"schema: relations carry no properties (got {sorted(candidate.properties)})"
        )
    return failures, candidate.model_copy(update={"source": source, "target": target})


Status = Literal["pass", "reject", "conflict"]


@dataclass
class GateResult:
    candidate: AnyCandidate
    status: Status
    failures: list[str] = field(default_factory=list)
    conflict: str | None = None


@dataclass
class _Batch:
    """Facts already accepted earlier in the same batch."""

    props: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # (relation, source label, source match key) -> (target label, target key) and vice versa
    targets: dict[tuple[str, str, str], tuple[str, str]] = field(default_factory=dict)
    sources: dict[tuple[str, str, str], tuple[str, str]] = field(default_factory=dict)


def _fmt(value: Any) -> str:
    return repr(value.isoformat() if isinstance(value, date) else value)


def _entity_conflict(
    c: CandidateEntity, cls: ClassSpec, graph: GraphView, batch: _Batch
) -> str | None:
    key_value = str(c.properties[cls.identity_key])
    label = neo4j_label(cls)
    existing = graph.node_properties(label, cls.identity_key, key_value)
    for origin, known in (
        ("in the graph", existing),
        ("in this batch", batch.props.get((label, match_key(key_value)))),
    ):
        if not known:
            continue
        for name, value in c.properties.items():
            if value is None or known.get(name) is None:
                continue
            if match_key(known[name]) != match_key(value):
                return (
                    f"{cls.name} {key_value!r}: property {name!r} is {_fmt(known[name])} "
                    f"{origin}, candidate says {_fmt(value)}"
                )
    return None


def _keys_under(spec: OntologySpec, class_name: str) -> dict[str, str]:
    """Neo4j label -> identity key for ``class_name`` and every subclass of it.

    A relation whose range is Organisation can point to an Issuer node, so cardinality must
    look at edges to any of these labels, not only the candidate's own class.
    """
    return {neo4j_label(c): c.identity_key for c in spec.classes if _is_a(spec, c.name, class_name)}


def _same_node(node: Mapping[str, str], label: str, key: str) -> bool:
    return label in node and match_key(node[label]) == match_key(key)


def _describe(node: Mapping[str, str]) -> str:
    label, value = sorted(node.items())[0]
    return f"{label} {value!r}"


def _relation_conflict(
    c: CandidateRelation, rel: RelationSpec, spec: OntologySpec, graph: GraphView, batch: _Batch
) -> str | None:
    by_name = {x.name: x for x in spec.classes}
    src_cls, tgt_cls = by_name[c.source.class_name], by_name[c.target.class_name]
    s_label, t_label = neo4j_label(src_cls), neo4j_label(tgt_cls)
    src = (s_label, src_cls.identity_key, c.source.key)
    tgt = (t_label, tgt_cls.identity_key, c.target.key)
    head = f"{rel.name} is {rel.cardinality}:"
    if rel.cardinality in ("many_to_one", "one_to_one"):
        for node in graph.targets(rel.name, src, _keys_under(spec, rel.range)):
            if not _same_node(node, t_label, c.target.key):
                return (
                    f"{head} {c.source.class_name} {c.source.key!r} already points to "
                    f"{_describe(node)} in the graph, candidate says {t_label} {c.target.key!r}"
                )
        prior = batch.targets.get((rel.name, s_label, match_key(c.source.key)))
        if prior is not None and not _same_node({prior[0]: prior[1]}, t_label, c.target.key):
            return (
                f"{head} {c.source.class_name} {c.source.key!r} points to {prior[0]} "
                f"{prior[1]!r} in this batch, candidate says {t_label} {c.target.key!r}"
            )
    if rel.cardinality in ("one_to_many", "one_to_one"):
        for node in graph.sources(rel.name, tgt, _keys_under(spec, rel.domain)):
            if not _same_node(node, s_label, c.source.key):
                return (
                    f"{head} {c.target.class_name} {c.target.key!r} already has source "
                    f"{_describe(node)} in the graph, candidate says {s_label} {c.source.key!r}"
                )
        prior = batch.sources.get((rel.name, t_label, match_key(c.target.key)))
        if prior is not None and not _same_node({prior[0]: prior[1]}, s_label, c.source.key):
            return (
                f"{head} {c.target.class_name} {c.target.key!r} has source {prior[0]} "
                f"{prior[1]!r} in this batch, candidate says {s_label} {c.source.key!r}"
            )
    return None


def check_batch(
    candidates: list[AnyCandidate], spec: OntologySpec, graph: GraphView
) -> list[GateResult]:
    """Run both gates over a batch, in order. Later candidates are checked against earlier
    ones that passed, so a batch can't contradict itself."""
    batch = _Batch()
    by_name = {c.name: c for c in spec.classes}
    results: list[GateResult] = []
    for raw in candidates:
        failures, cand = schema_gate(raw, spec)
        if failures:
            results.append(GateResult(cand, "reject", failures))
            continue
        if isinstance(cand, CandidateEntity):
            cls = by_name[cand.class_name]
            conflict = _entity_conflict(cand, cls, graph, batch)
            if conflict is None:
                slot = batch.props.setdefault(
                    (neo4j_label(cls), match_key(cand.properties[cls.identity_key])), {}
                )
                slot.update({k: v for k, v in cand.properties.items() if v is not None})
        else:
            rel = next(r for r in spec.relations if r.name == cand.relation)
            conflict = _relation_conflict(cand, rel, spec, graph, batch)
            if conflict is None:
                s_label = neo4j_label(by_name[cand.source.class_name])
                t_label = neo4j_label(by_name[cand.target.class_name])
                batch.targets.setdefault(
                    (rel.name, s_label, match_key(cand.source.key)), (t_label, cand.target.key)
                )
                batch.sources.setdefault(
                    (rel.name, t_label, match_key(cand.target.key)), (s_label, cand.source.key)
                )
        results.append(GateResult(cand, "conflict" if conflict else "pass", conflict=conflict))
    return results
