"""Admin JSON API (issue #14): everything the admin UI needs.

Roles: ``reviewer`` can read and work the review queue; everything that changes the standard
(ontology, mappings, scoring, users) or removes data (rollback) needs ``admin``. Every
state-changing call is CSRF-protected (``current_user``) and audited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select

from writer_service.api.deps import LLM_HINT, GraphSchemaDep, PipelineDep, ReaderDep, SupersetDep
from writer_service.auth.deps import CurrentUser, EngineDep, require_role
from writer_service.auth.users import create_user, get_user
from writer_service.governance.models import (
    AuditLog,
    Candidate,
    ColumnMapping,
    ExtensionProposal,
    IngestionRun,
    OntologyClass,
    PropertyDef,
    RelationType,
    ReviewItem,
    User,
    utcnow,
)
from writer_service.governance.uow import UnitOfWork
from writer_service.ingestion.superset import (
    DatasetInfo,
    DatasetMapping,
    DatasetNotFound,
    MappingError,
    SourceReadError,
    SupersetClient,
    SupersetError,
    approve_mapping,
    save_draft,
    suggest_mapping,
)
from writer_service.ontology.service import (
    OntologyError,
    OntologyService,
    new_draft_in,
    spec_of,
)
from writer_service.pipeline.orchestrator import PipelineError
from writer_service.scoring.policy import ScoringPolicy
from writer_service.scoring.store import activate_policy, active_policy

Reviewer = Annotated[CurrentUser, Depends(require_role("reviewer"))]
Admin = Annotated[CurrentUser, Depends(require_role("admin"))]

router = APIRouter(prefix="/api", tags=["admin"])


def _404(what: str) -> HTTPException:
    return HTTPException(404, f"{what} not found")


# =========================================================================== ontology


class PropertyIn(BaseModel):
    name: str
    datatype: Literal["string", "int", "float", "date", "bool"] = "string"
    required: bool = False
    description: str | None = None


def _unique_names(props: list[PropertyIn]) -> list[PropertyIn]:
    names = [p.name for p in props]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"duplicate property names: {', '.join(dupes)}")
    return props


class ClassIn(BaseModel):
    name: str
    identity_key: str
    namespace: Literal["core", "ext"] = "ext"
    parent: str | None = None
    description: str | None = None
    properties: list[PropertyIn] = Field(min_length=1)

    _no_dupes = field_validator("properties")(_unique_names)


class ClassPatch(BaseModel):
    description: str | None = None
    deprecated: bool | None = None
    add_properties: list[PropertyIn] = Field(default_factory=list)

    _no_dupes = field_validator("add_properties")(_unique_names)


class RelationIn(BaseModel):
    name: str
    domain: str
    range: str
    cardinality: Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"] = (
        "many_to_many"
    )
    namespace: Literal["core", "ext"] = "ext"
    description: str | None = None


class RelationPatch(BaseModel):
    description: str | None = None
    deprecated: bool | None = None


class VersionOut(BaseModel):
    version: int
    status: str
    notes: str | None
    created_by: str
    created_at: datetime
    published_at: datetime | None


class VersionDetail(VersionOut):
    classes: list[dict[str, Any]]
    relations: list[dict[str, Any]]


def _service(engine: Any, graph: Any) -> OntologyService:
    return OntologyService(engine, graph)


def _draft(uow: UnitOfWork, version: int) -> Any:
    v = uow.ontology_versions.get_by_number(version)
    if v is None:
        raise _404(f"version {version}")
    if v.status != "draft":
        raise HTTPException(409, f"v{version} is {v.status}; only drafts can be edited")
    return v


@router.get("/ontology/versions", response_model=list[VersionOut])
def list_versions(_: Reviewer, engine: EngineDep) -> list[VersionOut]:
    with UnitOfWork(engine) as uow:
        return [
            VersionOut.model_validate(v, from_attributes=True)
            for v in sorted(uow.ontology_versions.list(), key=lambda v: v.version)
        ]


@router.get("/ontology/versions/{version}", response_model=VersionDetail)
def get_version(version: int, _: Reviewer, engine: EngineDep) -> VersionDetail:
    with UnitOfWork(engine) as uow:
        v = uow.ontology_versions.get_by_number(version)
        if v is None:
            raise _404(f"version {version}")
        spec = spec_of(v)
        base = VersionOut.model_validate(v, from_attributes=True).model_dump()
    return VersionDetail(
        **base,
        classes=[
            {**c.__dict__, "properties": [p.__dict__ for p in c.properties]} for c in spec.classes
        ],
        relations=[r.__dict__ for r in spec.relations],
    )


@router.post("/ontology/drafts", status_code=201)
def create_draft(user: Admin, engine: EngineDep, graph: GraphSchemaDep) -> dict[str, int]:
    try:
        return {"version": _service(engine, graph).new_draft(user.username)}
    except OntologyError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/ontology/versions/{version}/classes", status_code=201)
def add_class(version: int, body: ClassIn, user: Admin, engine: EngineDep) -> dict[str, str]:
    with UnitOfWork(engine) as uow:
        _add_class(uow, version, body, user.username)
    return {"name": body.name}


def _add_class(uow: UnitOfWork, version: int, body: ClassIn, actor: str) -> None:
    v = _draft(uow, version)
    if any(c.name == body.name for c in v.classes):
        raise HTTPException(409, f"class '{body.name}' already exists in v{version}")
    parent = next((c for c in v.classes if c.name == body.parent), None)
    if body.parent and parent is None:
        raise HTTPException(422, f"unknown parent '{body.parent}'")
    row = OntologyClass(
        namespace=body.namespace,
        name=body.name,
        identity_key=body.identity_key,
        description=body.description,
        parent=parent,
    )
    row.properties = [PropertyDef(**p.model_dump()) for p in body.properties]
    v.classes.append(row)
    uow.audit(actor, "ontology.class.add", "ontology_version", str(version), body.model_dump())


@router.patch("/ontology/versions/{version}/classes/{name}")
def patch_class(
    version: int, name: str, body: ClassPatch, user: Admin, engine: EngineDep
) -> dict[str, str]:
    with UnitOfWork(engine) as uow:
        v = _draft(uow, version)
        row = next((c for c in v.classes if c.name == name), None)
        if row is None:
            raise _404(f"class '{name}'")
        if body.description is not None:
            row.description = body.description
        if body.deprecated is not None:
            row.deprecated = body.deprecated
        existing = {p.name for p in row.properties}
        for p in body.add_properties:
            if p.name in existing:
                raise HTTPException(409, f"property '{p.name}' already exists on '{name}'")
            row.properties.append(PropertyDef(**p.model_dump()))
        uow.audit(
            user.username,
            "ontology.class.update",
            "ontology_version",
            str(version),
            {"class": name, **body.model_dump(exclude_none=True)},
        )
    return {"name": name}


@router.post("/ontology/versions/{version}/relations", status_code=201)
def add_relation(version: int, body: RelationIn, user: Admin, engine: EngineDep) -> dict[str, str]:
    with UnitOfWork(engine) as uow:
        _add_relation(uow, version, body, user.username)
    return {"name": body.name}


def _add_relation(uow: UnitOfWork, version: int, body: RelationIn, actor: str) -> None:
    v = _draft(uow, version)
    if any(r.name == body.name for r in v.relation_types):
        raise HTTPException(409, f"relation '{body.name}' already exists in v{version}")
    v.relation_types.append(
        RelationType(
            namespace=body.namespace,
            name=body.name,
            domain_class=body.domain,
            range_class=body.range,
            cardinality=body.cardinality,
            description=body.description,
        )
    )
    uow.audit(actor, "ontology.relation.add", "ontology_version", str(version), body.model_dump())


@router.patch("/ontology/versions/{version}/relations/{name}")
def patch_relation(
    version: int, name: str, body: RelationPatch, user: Admin, engine: EngineDep
) -> dict[str, str]:
    with UnitOfWork(engine) as uow:
        v = _draft(uow, version)
        row = next((r for r in v.relation_types if r.name == name), None)
        if row is None:
            raise _404(f"relation '{name}'")
        if body.description is not None:
            row.description = body.description
        if body.deprecated is not None:
            row.deprecated = body.deprecated
        uow.audit(
            user.username,
            "ontology.relation.update",
            "ontology_version",
            str(version),
            {"relation": name, **body.model_dump(exclude_none=True)},
        )
    return {"name": name}


@router.post("/ontology/versions/{version}/publish")
def publish(version: int, user: Admin, engine: EngineDep, graph: GraphSchemaDep) -> dict[str, int]:
    try:
        _service(engine, graph).publish(version, user.username)
    except OntologyError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"published": version}


@router.post("/ontology/rollback")
def rollback_ontology(user: Admin, engine: EngineDep, graph: GraphSchemaDep) -> dict[str, int]:
    try:
        return {"published": _service(engine, graph).rollback(user.username)}
    except OntologyError as exc:
        raise HTTPException(409, str(exc)) from exc


# =========================================================================== proposals


class ProposalIn(BaseModel):
    kind: Literal["class", "relation"]
    payload: dict[str, Any]
    rationale: str | None = None


class ProposalOut(BaseModel):
    id: int
    kind: str
    payload: dict[str, Any]
    rationale: str | None
    proposed_by: str
    status: str
    decided_by: str | None
    created_at: datetime


@router.get("/proposals", response_model=list[ProposalOut])
def list_proposals(_: Reviewer, engine: EngineDep, status: str | None = None) -> list[ProposalOut]:
    with UnitOfWork(engine) as uow:
        q = select(ExtensionProposal).order_by(ExtensionProposal.id.desc())
        if status:
            q = q.where(ExtensionProposal.status == status)
        return [ProposalOut.model_validate(p, from_attributes=True) for p in uow.session.scalars(q)]


@router.post("/proposals", status_code=201)
def create_proposal(body: ProposalIn, user: Reviewer, engine: EngineDep) -> dict[str, int]:
    model: type[BaseModel] = ClassIn if body.kind == "class" else RelationIn
    try:  # 422 early if the payload can't become a definition
        model.model_validate(body.payload)
    except ValidationError as exc:
        raise HTTPException(422, f"invalid {body.kind} proposal: {exc.errors()}") from exc
    with UnitOfWork(engine) as uow:
        row = ExtensionProposal(
            kind=body.kind, payload=body.payload, rationale=body.rationale, proposed_by="user"
        )
        uow.session.add(row)
        uow.session.flush()
        uow.audit(user.username, "proposal.create", "extension_proposal", str(row.id))
        return {"id": row.id}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: int, user: Admin, engine: EngineDep) -> dict[str, int]:
    """Adds the proposal to the current draft (creating one from the published version if
    needed), in the ``ext`` namespace. It reaches the graph when the draft is published.
    One transaction: if any step fails, no draft is created and the proposal stays pending."""
    with UnitOfWork(engine) as uow:
        p = uow.session.get(ExtensionProposal, proposal_id)
        if p is None or p.status != "pending":
            raise HTTPException(409, f"proposal {proposal_id} is not pending")
        payload = {**p.payload, "namespace": "ext"}
        try:  # validate before touching any draft
            body: ClassIn | RelationIn = (
                ClassIn.model_validate(payload)
                if p.kind == "class"
                else RelationIn.model_validate(payload)
            )
        except ValidationError as exc:
            raise HTTPException(422, f"proposal payload is invalid: {exc}") from exc
        draft = next((v for v in uow.ontology_versions.list() if v.status == "draft"), None)
        try:
            version = draft.version if draft else new_draft_in(uow, user.username)
        except OntologyError as exc:
            raise HTTPException(409, str(exc)) from exc
        if isinstance(body, ClassIn):
            _add_class(uow, version, body, user.username)
        else:
            _add_relation(uow, version, body, user.username)
        p.status, p.decided_by, p.decided_at = "approved", user.username, utcnow()
        uow.audit(
            user.username,
            "proposal.approve",
            "extension_proposal",
            str(proposal_id),
            {"draft": version},
        )
    return {"draft": version}


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, user: Admin, engine: EngineDep) -> dict[str, str]:
    with UnitOfWork(engine) as uow:
        p = uow.session.get(ExtensionProposal, proposal_id)
        if p is None or p.status != "pending":
            raise HTTPException(409, f"proposal {proposal_id} is not pending")
        p.status, p.decided_by, p.decided_at = "rejected", user.username, utcnow()
        uow.audit(user.username, "proposal.reject", "extension_proposal", str(proposal_id))
    return {"status": "rejected"}


# =========================================================================== review queue


class ReviewOut(BaseModel):
    id: int
    reason: str
    status: str
    conflict_with: dict[str, Any] | None
    reviewer: str | None
    note: str | None
    created_at: datetime
    run_id: str
    source_type: str
    source_ref: str
    candidate: dict[str, Any]
    evidence: list[Any]
    score: float | None
    breakdown: dict[str, Any]


class ApproveIn(BaseModel):
    edited: dict[str, Any] | None = None


class RejectIn(BaseModel):
    note: str | None = None


@router.get("/review", response_model=list[ReviewOut])
def list_review(
    _: Reviewer,
    engine: EngineDep,
    status: str = "open",
    reason: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ReviewOut]:
    with UnitOfWork(engine) as uow:
        q = (
            select(ReviewItem)
            .where(ReviewItem.status == status)
            .order_by(ReviewItem.id)
            .limit(limit)
        )
        if reason:
            q = q.where(ReviewItem.reason == reason)
        out = []
        for item in uow.session.scalars(q):
            c, run = item.candidate, item.candidate.run
            out.append(
                ReviewOut(
                    id=item.id,
                    reason=item.reason,
                    status=item.status,
                    conflict_with=item.conflict_with,
                    reviewer=item.reviewer,
                    note=item.note,
                    created_at=item.created_at,
                    run_id=run.id,
                    source_type=run.source_type,
                    source_ref=run.source_ref,
                    candidate=c.payload,
                    evidence=c.evidence,
                    score=c.score,
                    breakdown=c.breakdown,
                )
            )
        return out


@router.post("/review/{item_id}/approve")
def approve_review(
    item_id: int, body: ApproveIn, user: Reviewer, pipeline: PipelineDep
) -> dict[str, str]:
    try:
        pipeline.approve(item_id, user.username, body.edited)
    except ValidationError as exc:  # `edited` isn't a well-formed candidate fact
        raise HTTPException(422, f"edited fact is invalid: {exc}") from exc
    except PipelineError as exc:  # not open, or the edit fails the gates
        raise HTTPException(409, str(exc)) from exc
    return {"status": "approved"}


@router.post("/review/{item_id}/reject")
def reject_review(
    item_id: int, body: RejectIn, user: Reviewer, pipeline: PipelineDep
) -> dict[str, str]:
    try:
        pipeline.reject(item_id, user.username, body.note)
    except PipelineError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "rejected"}


# =========================================================================== mappings


class MappingOut(BaseModel):
    id: int
    dataset_id: int
    dataset_name: str
    mapping: dict[str, Any]
    status: str
    suggested_by: str
    approved_by: str | None
    created_at: datetime


class MappingIn(BaseModel):
    dataset_id: int
    mapping: DatasetMapping


def _published_spec(uow: UnitOfWork) -> tuple[Any, int]:
    pub = uow.ontology_versions.published()
    if pub is None:
        raise HTTPException(409, "no published ontology")
    return spec_of(pub), pub.version


@router.get("/mappings", response_model=list[MappingOut])
def list_mappings(_: Admin, engine: EngineDep) -> list[MappingOut]:
    with UnitOfWork(engine) as uow:
        rows = uow.session.scalars(select(ColumnMapping).order_by(ColumnMapping.id.desc()))
        return [MappingOut.model_validate(r, from_attributes=True) for r in rows]


def _dataset(superset: SupersetClient, dataset_id: int) -> DatasetInfo:
    try:
        return superset.dataset(dataset_id)
    except DatasetNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except SupersetError as exc:  # unreachable, login failed, bad metadata
        raise HTTPException(502, str(exc)) from exc


@router.post("/mappings", status_code=201)
def create_mapping(
    body: MappingIn, user: Admin, engine: EngineDep, superset: SupersetDep
) -> dict[str, int]:
    ds = _dataset(superset, body.dataset_id)
    with UnitOfWork(engine) as uow:
        return {
            "id": save_draft(uow, ds, body.mapping, suggested_by="user", actor=user.username).id
        }


@router.post("/mappings/suggest/{dataset_id}", status_code=201)
def suggest(
    dataset_id: int,
    user: Admin,
    engine: EngineDep,
    superset: SupersetDep,
    pipeline: PipelineDep,
    reader: ReaderDep,
) -> dict[str, Any]:
    if pipeline.llm is None:
        raise HTTPException(503, f"mapping suggestions need an LLM; {LLM_HINT}")
    ds = _dataset(superset, dataset_id)
    with UnitOfWork(engine) as uow:
        spec, version = _published_spec(uow)
    try:
        sample = list(reader.rows(ds, limit=5))
    except SourceReadError as exc:  # names the dataset only, never SQL or the driver
        raise HTTPException(502, str(exc)) from exc
    draft = suggest_mapping(pipeline.llm, spec, version, ds, sample)
    with UnitOfWork(engine) as uow:
        row = save_draft(uow, ds, draft, suggested_by="llm", actor=user.username)
        return {"id": row.id, "mapping": draft.model_dump()}


@router.post("/mappings/{mapping_id}/approve")
def approve(
    mapping_id: int, user: Admin, engine: EngineDep, superset: SupersetDep
) -> dict[str, str]:
    with UnitOfWork(engine) as uow:
        row = uow.session.get(ColumnMapping, mapping_id)
        if row is None:
            raise _404(f"mapping {mapping_id}")
        dataset_id = row.dataset_id
    ds = _dataset(superset, dataset_id)
    try:
        with UnitOfWork(engine) as uow:
            spec, _ = _published_spec(uow)
            approve_mapping(uow, mapping_id, spec, ds, actor=user.username)
    except MappingError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"status": "approved"}


# =========================================================================== scoring


@router.get("/scoring", response_model=ScoringPolicy)
def get_scoring(_: Reviewer, engine: EngineDep) -> ScoringPolicy:
    with UnitOfWork(engine) as uow:
        return active_policy(uow)


@router.put("/scoring", response_model=ScoringPolicy)
def put_scoring(body: ScoringPolicy, user: Admin, engine: EngineDep) -> ScoringPolicy:
    with UnitOfWork(engine) as uow:
        activate_policy(uow, body, actor=user.username)
    return body


# =========================================================================== runs + audit


class RunOut(BaseModel):
    id: str
    source_type: str
    source_ref: str
    ontology_version: int
    status: str
    counts: dict[str, Any]
    error: str | None
    started_by: str | None
    started_at: datetime
    finished_at: datetime | None


class RunDetail(RunOut):
    candidates: list[dict[str, Any]]


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    _: Reviewer, engine: EngineDep, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[RunOut]:
    with UnitOfWork(engine) as uow:
        q = select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(limit)
        return [RunOut.model_validate(r, from_attributes=True) for r in uow.session.scalars(q)]


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, _: Reviewer, engine: EngineDep) -> RunDetail:
    with UnitOfWork(engine) as uow:
        run = uow.session.get(IngestionRun, run_id)
        if run is None:
            raise _404(f"run {run_id}")
        cands = uow.session.scalars(
            select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)
        )
        return RunDetail(
            **RunOut.model_validate(run, from_attributes=True).model_dump(),
            candidates=[
                {
                    "id": c.id,
                    "kind": c.kind,
                    "decision": c.decision,
                    "score": c.score,
                    "payload": c.payload,
                    "gate_failures": c.gate_failures,
                    "breakdown": c.breakdown,
                }
                for c in cands
            ],
        )


@router.post("/runs/{run_id}/rollback")
def rollback_run(run_id: str, user: Admin, pipeline: PipelineDep) -> dict[str, int]:
    try:
        return pipeline.rollback(run_id, user.username)
    except PipelineError as exc:
        raise HTTPException(409, str(exc)) from exc


class AuditOut(BaseModel):
    id: int
    at: datetime
    actor: str
    action: str
    entity_type: str
    entity_id: str
    detail: dict[str, Any]


@router.get("/audit", response_model=list[AuditOut])
def list_audit(
    _: Admin, engine: EngineDep, limit: Annotated[int, Query(ge=1, le=1000)] = 200
) -> list[AuditOut]:
    with UnitOfWork(engine) as uow:
        q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        return [AuditOut.model_validate(a, from_attributes=True) for a in uow.session.scalars(q)]


# =========================================================================== users


class UserOut(BaseModel):
    username: str
    role: str
    active: bool
    created_at: datetime


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=1024)
    role: Literal["admin", "reviewer"]


class UserPatch(BaseModel):
    role: Literal["admin", "reviewer"] | None = None
    active: bool | None = None


@router.get("/users", response_model=list[UserOut])
def list_users(_: Admin, engine: EngineDep) -> list[UserOut]:
    with UnitOfWork(engine) as uow:
        return [
            UserOut.model_validate(u, from_attributes=True)
            for u in uow.session.scalars(select(User).order_by(User.username))
        ]


@router.post("/users", status_code=201, response_model=UserOut)
def add_user(body: UserIn, user: Admin, engine: EngineDep) -> UserOut:
    try:
        with UnitOfWork(engine) as uow:
            row = create_user(uow, body.username, body.password, body.role, actor=user.username)
            uow.session.flush()
            return UserOut.model_validate(row, from_attributes=True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/users/{username}", response_model=UserOut)
def patch_user(username: str, body: UserPatch, user: Admin, engine: EngineDep) -> UserOut:
    if username == user.username and (body.active is False or body.role == "reviewer"):
        raise HTTPException(409, "admins can't deactivate or demote themselves")
    with UnitOfWork(engine) as uow:
        row = get_user(uow, username)
        if row is None:
            raise _404(f"user {username}")
        if body.role is not None:
            row.role = body.role
        if body.active is not None:
            row.active = body.active
            row.session_version += 1  # end their sessions now
        uow.audit(
            user.username, "user.update", "user", username, body.model_dump(exclude_none=True)
        )
        return UserOut.model_validate(row, from_attributes=True)
