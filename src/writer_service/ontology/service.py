"""Ontology lifecycle: bootstrap, draft, publish (through the gate) and rollback.

Publish/rollback order: lock versions → validate → all SQL writes (flushed) → sync Neo4j
constraints → commit. Neo4j DDL can't join the SQL transaction, so if *anything* fails after
the sync has started (the sync itself, the audit update or the commit), the service re-syncs
Neo4j back to the still-published version's constraints and the SQL transaction rolls back.
Sync creates before it drops, so even a failed restore only leaves *extra* constraints. Only a
process crash between sync and commit can leave Neo4j ahead of the governance store; retrying
the command (DDL is idempotent) or publishing again fixes it.

Concurrency: publish, rollback and new_draft lock the ``ontology_versions`` rows
(``SELECT ... FOR UPDATE`` on MySQL), so two admins cannot publish at the same time.
SQLite has no row locks; use MySQL for anything multi-user.
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
from writer_service.ontology.neo4j_schema import (
    GraphSchema,
    SyncResult,
    sync_schema,
)
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


def new_draft_in(uow: UnitOfWork, actor: str) -> int:
    """``new_draft`` inside the caller's transaction (so it can be part of a larger change)."""
    versions = uow.ontology_versions.lock_all()
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
    uow.session.flush()
    uow.audit(actor, "ontology.draft", "ontology_version", str(number), {"from": pub.version})
    return number


class OntologyService:
    def __init__(self, engine: Engine, graph: GraphSchema) -> None:
        self.engine = engine
        self.graph = graph

    def _sync(self, target: OntologySpec, fallback: OntologySpec | None) -> SyncResult:
        """Sync Neo4j to ``target``; on failure restore ``fallback`` (best effort) and re-raise."""
        try:
            return sync_schema(self.graph, target)
        except Exception as exc:
            self._restore(fallback, exc)
            raise

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

    def sync(self, actor: str) -> SyncResult:
        """Re-apply the published version's Neo4j schema (constraints and full-text indexes),
        e.g. after an upgrade adds new managed indexes. Idempotent. Holds the version lock
        like ``publish``, so a concurrent publish can't be undone by a stale sync."""
        with UnitOfWork(self.engine) as uow:
            uow.ontology_versions.lock_all()
            pub = uow.ontology_versions.published()
            if pub is None:
                raise OntologyError("nothing is published (run: writer-ontology bootstrap)")
            spec, version = spec_of(pub), pub.version
            result = self._sync(spec, None)
            uow.audit(
                actor,
                "ontology.sync",
                "ontology_version",
                str(version),
                {"created": result.created, "dropped": result.dropped},
            )
            return result

    def new_draft(self, actor: str) -> int:
        """Start a new draft as a copy of the published version."""
        with UnitOfWork(self.engine) as uow:
            return new_draft_in(uow, actor)

    def publish(self, number: int, actor: str) -> None:
        synced_to: OntologySpec | None = None
        fallback: OntologySpec | None = None
        try:
            with UnitOfWork(self.engine) as uow:
                uow.ontology_versions.lock_all()
                version = uow.ontology_versions.get_by_number(number)
                if version is None:
                    raise OntologyError(f"no version {number}")
                if version.status != "draft":
                    raise OntologyError(f"v{number} is not a draft (status {version.status})")
                previous = uow.ontology_versions.published()
                spec = spec_of(version)
                fallback = spec_of(previous) if previous else None
                errors = validate(spec, previous=fallback)
                if errors:
                    raise OntologyError(f"v{number} failed validation: " + "; ".join(errors))
                # All SQL writes first, so the only thing left after the Neo4j sync is commit.
                if previous is not None:
                    previous.status = "superseded"
                    uow.session.flush()  # keep at most one published row at every point
                version.status = "published"
                version.published_at = utcnow()
                entry = uow.audit(
                    actor,
                    "ontology.publish",
                    "ontology_version",
                    str(number),
                    {"previous": previous.version if previous else None},
                )
                uow.session.flush()
                result = self._sync(spec, fallback)
                synced_to = spec
                entry.detail = {
                    **entry.detail,
                    "constraints_created": result.created,
                    "constraints_dropped": result.dropped,
                }
        except BaseException as exc:
            # The sync succeeded but the governance transaction did not commit: put Neo4j back.
            if synced_to is not None:
                self._restore(fallback, exc)
            raise

    def _restore(self, spec: OntologySpec | None, exc: BaseException) -> None:
        if spec is None:
            return
        try:
            sync_schema(self.graph, spec)
        except Exception as restore_exc:
            exc.add_note(f"restoring previous constraints also failed: {restore_exc}")

    def rollback(self, actor: str) -> int:
        """Deprecate the published version and re-publish the most recent superseded one."""
        synced = False
        fallback: OntologySpec | None = None
        try:
            with UnitOfWork(self.engine) as uow:
                versions = uow.ontology_versions.lock_all()
                current = uow.ontology_versions.published()
                if current is None:
                    raise OntologyError("nothing is published")
                candidates = [
                    v for v in versions if v.status == "superseded" and v.version < current.version
                ]
                if not candidates:
                    raise OntologyError("nothing to roll back to")
                target = max(candidates, key=lambda v: v.version)
                fallback = spec_of(current)
                current.status = "deprecated"
                uow.session.flush()
                target.status = "published"
                target.published_at = utcnow()
                entry = uow.audit(
                    actor,
                    "ontology.rollback",
                    "ontology_version",
                    str(target.version),
                    {"from": current.version},
                )
                uow.session.flush()
                result = self._sync(spec_of(target), fallback)
                synced = True
                entry.detail = {
                    **entry.detail,
                    "constraints_created": result.created,
                    "constraints_dropped": result.dropped,
                }
                rolled_back_to = target.version
        except BaseException as exc:
            if synced:
                self._restore(fallback, exc)
            raise
        return rolled_back_to
