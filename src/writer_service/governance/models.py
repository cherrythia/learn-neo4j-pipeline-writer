"""Governance store: the relational home of the ontology, review queue, config and audit.

Neo4j holds only accepted facts; everything about *how* facts are governed lives here
(ADR-0006). Schema changes go through Alembic migrations in ``governance/migrations``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum(name: str, *values: str) -> Enum:
    # VARCHAR + CHECK constraint: portable across MySQL and SQLite, validated by the DB.
    return Enum(*values, name=name, native_enum=False, create_constraint=True, length=20)


VERSION_STATUS = _enum("version_status", "draft", "published", "superseded", "deprecated")
NAMESPACE = _enum("namespace", "core", "ext")
DATATYPE = _enum("datatype", "string", "int", "float", "date", "bool")
CARDINALITY = _enum("cardinality", "one_to_one", "many_to_one", "one_to_many", "many_to_many")
PROPOSAL_KIND = _enum("proposal_kind", "class", "relation")
PROPOSAL_STATUS = _enum("proposal_status", "pending", "approved", "rejected")
PROPOSED_BY = _enum("proposed_by", "llm", "user")
SOURCE_TYPE = _enum("source_type", "pdf", "excel", "superset")
RUN_STATUS = _enum("run_status", "running", "completed", "failed", "rolled_back")
CANDIDATE_KIND = _enum("candidate_kind", "entity", "relation")
DECISION = _enum("decision", "commit", "review", "reject", "conflict")
REVIEW_REASON = _enum("review_reason", "score", "conflict")
REVIEW_STATUS = _enum("review_status", "open", "approved", "rejected", "edited")
MAPPING_STATUS = _enum("mapping_status", "draft", "approved", "retired")
ROLE = _enum("role", "admin", "reviewer")


class Base(DeclarativeBase):
    # Deterministic constraint names. MySQL requires CHECK names to be unique per schema,
    # and enums like ``namespace`` are shared by several tables, so the table name is included.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def _ts(nullable: bool = False) -> Mapped[Any]:
    """Timezone-aware timestamp column; non-nullable ones default to now (UTC)."""
    if nullable:
        return mapped_column(DateTime(timezone=True), nullable=True)
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# --------------------------------------------------------------------------- ontology


class OntologyVersion(Base):
    __tablename__ = "ontology_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    status: Mapped[str] = mapped_column(VERSION_STATUS, default="draft")
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = _ts()
    published_at: Mapped[datetime | None] = _ts(nullable=True)

    classes: Mapped[list[OntologyClass]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )
    relation_types: Mapped[list[RelationType]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class OntologyClass(Base):
    """A class in the taxonomy. ``parent`` gives the subclass-of hierarchy."""

    __tablename__ = "ontology_classes"
    __table_args__ = (UniqueConstraint("version_id", "namespace", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("ontology_versions.id", ondelete="CASCADE"))
    namespace: Mapped[str] = mapped_column(NAMESPACE, default="core")
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    identity_key: Mapped[str] = mapped_column(String(100))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("ontology_classes.id"))
    deprecated: Mapped[bool] = mapped_column(default=False)

    version: Mapped[OntologyVersion] = relationship(back_populates="classes")
    parent: Mapped[OntologyClass | None] = relationship(remote_side=[id])
    properties: Mapped[list[PropertyDef]] = relationship(
        back_populates="ontology_class", cascade="all, delete-orphan"
    )


class PropertyDef(Base):
    __tablename__ = "property_defs"
    __table_args__ = (UniqueConstraint("class_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("ontology_classes.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    datatype: Mapped[str] = mapped_column(DATATYPE, default="string")
    required: Mapped[bool] = mapped_column(default=False)
    description: Mapped[str | None] = mapped_column(Text)

    ontology_class: Mapped[OntologyClass] = relationship(back_populates="properties")


class RelationType(Base):
    """A typed edge. ``domain_class`` -> ``range_class`` by class name within the version."""

    __tablename__ = "relation_types"
    __table_args__ = (UniqueConstraint("version_id", "namespace", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("ontology_versions.id", ondelete="CASCADE"))
    namespace: Mapped[str] = mapped_column(NAMESPACE, default="core")
    name: Mapped[str] = mapped_column(String(100))
    domain_class: Mapped[str] = mapped_column(String(100))
    range_class: Mapped[str] = mapped_column(String(100))
    cardinality: Mapped[str] = mapped_column(CARDINALITY, default="many_to_many")
    description: Mapped[str | None] = mapped_column(Text)
    deprecated: Mapped[bool] = mapped_column(default=False)

    version: Mapped[OntologyVersion] = relationship(back_populates="relation_types")


class ExtensionProposal(Base):
    """A new class or relation suggested by the LLM or a user; needs human approval."""

    __tablename__ = "extension_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(PROPOSAL_KIND)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    rationale: Mapped[str | None] = mapped_column(Text)
    proposed_by: Mapped[str] = mapped_column(PROPOSED_BY)
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("ingestion_runs.id"))
    status: Mapped[str] = mapped_column(PROPOSAL_STATUS, default="pending")
    created_at: Mapped[datetime] = _ts()
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = _ts(nullable=True)


# --------------------------------------------------------------------------- scoring


class ScoringConfig(Base):
    __tablename__ = "scoring_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    weights: Mapped[dict[str, Any]] = mapped_column(JSON)
    commit_threshold: Mapped[float] = mapped_column(Float)
    review_threshold: Mapped[float] = mapped_column(Float)
    self_consistency_runs: Mapped[int] = mapped_column(Integer, default=3)
    active: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = _ts()


# --------------------------------------------------------------------------- runs


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_type: Mapped[str] = mapped_column(SOURCE_TYPE)
    source_ref: Mapped[str] = mapped_column(String(500))
    ontology_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(RUN_STATUS, default="running")
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = _ts(nullable=True)

    candidates: Mapped[list[Candidate]] = relationship(back_populates="run")


class Candidate(Base):
    """A candidate fact with its verification results. Never written to Neo4j directly."""

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"), index=True)
    kind: Mapped[str] = mapped_column(CANDIDATE_KIND)
    canonical_hash: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    score: Mapped[float | None] = mapped_column(Float)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    gate_failures: Mapped[list[Any]] = mapped_column(JSON, default=list)
    decision: Mapped[str | None] = mapped_column(DECISION)
    created_at: Mapped[datetime] = _ts()

    run: Mapped[IngestionRun] = relationship(back_populates="candidates")
    review_item: Mapped[ReviewItem | None] = relationship(back_populates="candidate")


class ReviewItem(Base):
    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), unique=True)
    reason: Mapped[str] = mapped_column(REVIEW_REASON)
    conflict_with: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(REVIEW_STATUS, default="open")
    reviewer: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    decided_at: Mapped[datetime | None] = _ts(nullable=True)

    candidate: Mapped[Candidate] = relationship(back_populates="review_item")


# --------------------------------------------------------------------------- sources


class ColumnMapping(Base):
    """Superset dataset column -> ontology mapping (LLM-suggested, admin-approved)."""

    __tablename__ = "column_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(Integer, index=True)
    dataset_name: Mapped[str] = mapped_column(String(200))
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(MAPPING_STATUS, default="draft")
    suggested_by: Mapped[str] = mapped_column(PROPOSED_BY)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts()


# --------------------------------------------------------------------------- people


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(ROLE)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _ts()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = _ts()
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
