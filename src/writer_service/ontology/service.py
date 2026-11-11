"""Ontology lifecycle: bootstrap, draft, publish (through the gate) and rollback.

Publish/rollback order: validate → sync Neo4j constraints → update version statuses → commit.
If the Neo4j sync fails, the governance transaction rolls back and nothing changes. Constraint
DDL is idempotent, so a crash after the sync but before the commit is fixed by retrying.
"""

from __future__ import annotations

from sqlalchemy import Engine, func, select

from writer_service.governance.models import (
    OntologyClass,
    OntologyVersion,
    PropertyDef,
    RelationType,
    utcnow,
)
from writer_service.governance.uow import UnitOfWork
from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.neo4j_schema import GraphSchema, desired_constraints, sync_constraints
from writer_service.ontology.spec import ClassSpec, OntologySpec, PropertySpec, RelationSpec
from writer_service.ontology.validation import validate


class OntologyError(Exception):
    pass


def spec_of(version: OntologyVersion) -> OntologySpec:
    classes = tuple(
        ClassSpec(
            name=c.name,
            identity_key=c.identity_key,
            properties=tuple(
                PropertySpec(p.name, p.datatype, p.required, p.description)  # type: ignore[arg-type]
                for p in sorted(c.properties, key=lambda p: p.name)
            ),
            parent=c.parent.name if c.parent else None,
            namespace=c.namespace,  # type: ignore[arg-type]
            description=c.description,
            deprecated=c.deprecated,
        )
        for c in sorted(version.classes, key=lambda c: c.name)
    )
    relations = tuple(
        RelationSpec(
            name=r.name,
            domain=r.domain_class,
            range=r.range_class,
            cardinality=r.cardinality,  # type: ignore[arg-type]
            namespace=r.namespace,  # type: ignore[arg-type]
            description=r.description,
            deprecated=r.deprecated,
        )
        for r in sorted(version.relation_types, key=lambda r: r.name)
    )
    return OntologySpec(classes=classes, relations=relations)


def _fill(version: OntologyVersion, spec: OntologySpec) -> None:
    """Create rows for ``spec`` under ``version``; parents are linked by name."""
    rows: dict[str, OntologyClass] = {}
    for c in spec.classes:
        row = OntologyClass(
            namespace=c.namespace,
            name=c.name,
            identity_key=c.identity_key,
            description=c.description,
            deprecated=c.deprecated,
        )
        row.properties = [
            PropertyDef(
                name=p.name, datatype=p.datatype, required=p.required, description=p.description
            )
            for p in c.properties
        ]
        rows[c.name] = row
        version.classes.append(row)
    for c in spec.classes:
        if c.parent is not None:
            rows[c.name].parent = rows[c.parent]
    for r in spec.relations:
        version.relation_types.append(
            RelationType(
                namespace=r.namespace,
                name=r.name,
                domain_class=r.domain,
                range_class=r.range,
                cardinality=r.cardinality,
                description=r.description,
                deprecated=r.deprecated,
            )
        )


class OntologyService:
    def __init__(self, engine: Engine, graph: GraphSchema) -> None:
        self.engine = engine
        self.graph = graph

    def published_spec(self) -> OntologySpec | None:
        with UnitOfWork(self.engine) as uow:
            pub = uow.ontology_versions.published()
            return spec_of(pub) if pub else None

    def bootstrap(self, actor: str) -> int:
        """Seed the core ontology as v1 and publish it. No-op if something is published."""
        with UnitOfWork(self.engine) as uow:
            pub = uow.ontology_versions.published()
            if pub is not None:
                return pub.version
            if uow.ontology_versions.get_by_number(1) is None:
                v1 = OntologyVersion(
                    version=1, status="draft", created_by=actor, notes="core ontology"
                )
                _fill(v1, OntologySpec(CORE_CLASSES, CORE_RELATIONS))
                uow.session.add(v1)
                uow.audit(actor, "ontology.seed", "ontology_version", "1")
        self.publish(1, actor)
        return 1

    def new_draft(self, actor: str) -> int:
        """Start a new draft as a copy of the published version."""
        with UnitOfWork(self.engine) as uow:
            versions = uow.ontology_versions.list()
            draft = next((v for v in versions if v.status == "draft"), None)
            if draft is not None:
                raise OntologyError(f"draft v{draft.version} already exists")
            pub = uow.ontology_versions.published()
            if pub is None:
                raise OntologyError("no published version; run bootstrap first")
            number = (uow.session.scalar(select(func.max(OntologyVersion.version))) or 0) + 1
            new = OntologyVersion(
                version=number, status="draft", created_by=actor, notes=f"draft from v{pub.version}"
            )
            _fill(new, spec_of(pub))
            uow.session.add(new)
            uow.audit(
                actor, "ontology.draft", "ontology_version", str(number), {"from": pub.version}
            )
            return number

    def publish(self, number: int, actor: str) -> None:
        with UnitOfWork(self.engine) as uow:
            version = uow.ontology_versions.get_by_number(number)
            if version is None:
                raise OntologyError(f"no version {number}")
            if version.status != "draft":
                raise OntologyError(f"v{number} is not a draft (status {version.status})")
            previous = uow.ontology_versions.published()
            spec = spec_of(version)
            errors = validate(spec, previous=spec_of(previous) if previous else None)
            if errors:
                raise OntologyError(f"v{number} failed validation: " + "; ".join(errors))
            result = sync_constraints(self.graph, desired_constraints(spec))
            if previous is not None:
                previous.status = "superseded"
                uow.session.flush()  # keep at most one published row at every point
            version.status = "published"
            version.published_at = utcnow()
            uow.audit(
                actor,
                "ontology.publish",
                "ontology_version",
                str(number),
                {
                    "previous": previous.version if previous else None,
                    "constraints_created": result.created,
                    "constraints_dropped": result.dropped,
                },
            )

    def rollback(self, actor: str) -> int:
        """Deprecate the published version and re-publish the most recent superseded one."""
        with UnitOfWork(self.engine) as uow:
            current = uow.ontology_versions.published()
            if current is None:
                raise OntologyError("nothing is published")
            candidates = [
                v
                for v in uow.ontology_versions.list()
                if v.status == "superseded" and v.version < current.version
            ]
            if not candidates:
                raise OntologyError("nothing to roll back to")
            target = max(candidates, key=lambda v: v.version)
            result = sync_constraints(self.graph, desired_constraints(spec_of(target)))
            current.status = "deprecated"
            uow.session.flush()
            target.status = "published"
            target.published_at = utcnow()
            uow.audit(
                actor,
                "ontology.rollback",
                "ontology_version",
                str(target.version),
                {
                    "from": current.version,
                    "constraints_created": result.created,
                    "constraints_dropped": result.dropped,
                },
            )
            return target.version
