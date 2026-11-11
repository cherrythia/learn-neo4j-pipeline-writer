"""Superset/MySQL ingestion (ADR-0008): structured rows become facts through an
**admin-approved column mapping**, deterministically; the LLM only drafts the mapping.

    Superset REST API ──► which table/SQL backs dataset N, and its columns
    MySQL (read-only) ──► rows, in pages
    approved mapping  ──► candidate entities/relations with row-level evidence
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx2
from pydantic import BaseModel, Field, create_model
from sqlalchemy import Engine, column, func, select, table, text

from writer_service.governance.models import ColumnMapping, utcnow
from writer_service.governance.uow import UnitOfWork
from writer_service.llm.prompts import PromptTemplate
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import (
    CandidateEntity,
    CandidateRelation,
    EntityRef,
    Evidence,
    canonical_text,
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class SupersetError(Exception):
    pass


# --------------------------------------------------------------------------- Superset API


@dataclass(frozen=True)
class DatasetInfo:
    id: int
    table_name: str
    schema: str | None
    sql: str | None  # virtual (SQL) dataset
    backend: str
    columns: tuple[str, ...]

    @property
    def ref(self) -> str:
        return f"superset:dataset:{self.id}"


class SupersetClient:
    """The two Superset endpoints we need. ``http`` is injectable (tests use the mock app)."""

    def __init__(
        self, base_url: str, username: str, password: str, http: httpx2.Client | None = None
    ) -> None:
        self.http = http or httpx2.Client(base_url=base_url, timeout=30)
        self.username, self.password = username, password
        self._token: str | None = None

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> SupersetClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            r = self.http.post(
                "/api/v1/security/login",
                json={
                    "username": self.username,
                    "password": self.password,
                    "provider": "db",
                    "refresh": False,
                },
            )
            if r.status_code != 200:
                raise SupersetError(f"Superset login failed ({r.status_code})")
            self._token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def dataset(self, dataset_id: int) -> DatasetInfo:
        r = self.http.get(f"/api/v1/dataset/{dataset_id}", headers=self._auth())
        if r.status_code == 404:
            raise SupersetError(f"Superset dataset {dataset_id} not found")
        if r.status_code != 200:
            raise SupersetError(f"Superset dataset {dataset_id}: HTTP {r.status_code}")
        d = r.json()["result"]
        cols = tuple(c["column_name"] for c in d.get("columns", []))
        for name in (d["table_name"], d.get("schema") or "x", *cols):
            if not _IDENT.match(name):
                raise SupersetError(f"unsupported identifier from Superset: {name!r}")
        return DatasetInfo(
            dataset_id,
            d["table_name"],
            d.get("schema"),
            d.get("sql"),
            d.get("database", {}).get("backend", "unknown"),
            cols,
        )


# --------------------------------------------------------------------------- MySQL reader


class DatasetReader:
    """Reads a dataset's rows in pages through a read-only connection."""

    def __init__(self, engine: Engine, page_size: int = 500) -> None:
        self.engine = engine
        self.page_size = page_size

    def rows(self, ds: DatasetInfo, limit: int | None = None) -> Iterator[dict[str, Any]]:
        if ds.sql:  # virtual dataset: Superset already holds the SQL; wrap it as a subquery
            source: Any = text(ds.sql).columns(*(column(c) for c in ds.columns)).subquery("ds")
            cols = [source.c[c] for c in ds.columns]
        else:  # identifiers were validated in SupersetClient and are quoted by SQLAlchemy
            source = table(ds.table_name, *(column(c) for c in ds.columns), schema=ds.schema)
            cols = list(source.c)
        order = cols[0]
        offset, seen = 0, 0
        with self.engine.connect() as conn:
            while True:
                page = (
                    conn.execute(
                        select(*cols)
                        .select_from(source)
                        .order_by(order)
                        .limit(self.page_size)
                        .offset(offset)
                    )
                    .mappings()
                    .all()
                )
                for row in page:
                    yield dict(row)
                    seen += 1
                    if limit is not None and seen >= limit:
                        return
                if len(page) < self.page_size:
                    return
                offset += self.page_size

    def count(self, ds: DatasetInfo) -> int:
        source = table(ds.table_name, *(column(c) for c in ds.columns), schema=ds.schema)
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(source)).scalar_one())


# --------------------------------------------------------------------------- mapping


class EntityMapping(BaseModel):
    class_name: str
    columns: dict[str, str]  # ontology property -> dataset column


class EndMapping(BaseModel):
    class_name: str
    key_column: str


class RelationMapping(BaseModel):
    relation: str
    source: EndMapping
    target: EndMapping


class DatasetMapping(BaseModel):
    entities: list[EntityMapping] = Field(default_factory=list)
    relations: list[RelationMapping] = Field(default_factory=list)


def validate_mapping(m: DatasetMapping, spec: OntologySpec, ds: DatasetInfo) -> list[str]:
    errors: list[str] = []
    classes = {c.name: c for c in spec.active_classes()}
    rels = {r.name: r for r in spec.relations if not r.deprecated}
    cols = set(ds.columns)
    if not m.entities and not m.relations:
        errors.append("mapping is empty")
    for e in m.entities:
        cls = classes.get(e.class_name)
        if cls is None:
            errors.append(f"unknown or deprecated class '{e.class_name}'")
            continue
        props = {p.name for p in cls.properties}
        for prop, col in e.columns.items():
            if prop not in props:
                errors.append(f"class '{cls.name}' has no property '{prop}'")
            if col not in cols:
                errors.append(f"dataset has no column '{col}'")
        if cls.identity_key not in e.columns:
            errors.append(f"'{cls.name}' mapping must map its identity key '{cls.identity_key}'")
    for r in m.relations:
        spec_rel = rels.get(r.relation)
        if spec_rel is None:
            errors.append(f"unknown or deprecated relation '{r.relation}'")
        for end in (r.source, r.target):
            if end.class_name not in classes:
                errors.append(f"unknown or deprecated class '{end.class_name}'")
            if end.key_column not in cols:
                errors.append(f"dataset has no column '{end.key_column}'")
    return errors


def row_text(row: dict[str, Any]) -> str:
    return "; ".join(f"{k}: {canonical_text(str(v))}" for k, v in row.items() if v is not None)


def row_locator(ds: DatasetInfo, row: dict[str, Any]) -> str:
    first = ds.columns[0]
    return f"row {first}={row.get(first)}"


def rows_to_candidates(
    m: DatasetMapping, ds: DatasetInfo, rows: list[dict[str, Any]]
) -> tuple[list[CandidateEntity | CandidateRelation], list[tuple[str, str, str]]]:
    """Deterministic: each row yields the mapped facts, each with one evidence item per mapped
    column (``column: value``, a verbatim fragment of the row text, so grounding is 1.0).
    Returns (candidates, texts)."""
    out: list[CandidateEntity | CandidateRelation] = []
    texts: list[tuple[str, str, str]] = []
    for row in rows:
        locator, body = row_locator(ds, row), row_text(row)
        texts.append((ds.ref, locator, body))

        def ev(*cols: str, locator: str = locator, row: dict[str, Any] = row) -> list[Evidence]:
            # One evidence item per column: each "column: value" is a contiguous, verbatim
            # fragment of the row text, so grounding is exact (non-adjacent columns joined
            # into one quote would not be verbatim).
            return [
                Evidence(
                    source_type="superset",
                    source_ref=ds.ref,
                    locator=locator,
                    quote=f"{c}: {canonical_text(str(row[c]))}",
                )
                for c in cols
                if row.get(c) is not None
            ]

        for e in m.entities:
            props = {p: row.get(c) for p, c in e.columns.items() if row.get(c) is not None}
            ordered = [c for c in ds.columns if c in e.columns.values()]
            out.append(
                CandidateEntity(class_name=e.class_name, properties=props, evidence=ev(*ordered))
            )
        for r in m.relations:
            s, t = row.get(r.source.key_column), row.get(r.target.key_column)
            if s is None or t is None:
                continue
            ordered = [c for c in ds.columns if c in (r.source.key_column, r.target.key_column)]
            out.append(
                CandidateRelation(
                    relation=r.relation,
                    source=EntityRef(class_name=r.source.class_name, key=str(s)),
                    target=EntityRef(class_name=r.target.class_name, key=str(t)),
                    evidence=ev(*ordered),
                )
            )
    return out, texts


# --------------------------------------------------------------------------- LLM draft


MAPPING_PROMPT = PromptTemplate(
    name="superset_mapping",
    version=1,
    system=(
        "You map database columns to a knowledge-graph ontology. Use ONLY the given classes, "
        "properties and relations. Map an entity only if its identity key column exists. "
        "Prefer leaving something unmapped over guessing."
    ),
    user="Ontology:\n{ontology}\n\nDataset '{table}' columns: {columns}\n\nSample rows:\n{rows}",
)


def suggest_mapping(
    llm: StructuredLLM,
    spec: OntologySpec,
    version: int,
    ds: DatasetInfo,
    sample: list[dict[str, Any]],
) -> DatasetMapping:
    """An LLM-drafted mapping, constrained to the ontology's names and the dataset's columns
    (schema enums). Always stored as a *draft*; an admin must approve it."""
    from writer_service.ingestion.documents import render_ontology

    classes = tuple(c.name for c in spec.active_classes())
    props = tuple(sorted({p.name for c in spec.active_classes() for p in c.properties}))
    relations = tuple(r.name for r in spec.relations if not r.deprecated) or ("NONE",)
    Cls: Any = Literal[classes]
    Prop: Any = Literal[props]
    Col: Any = Literal[ds.columns]
    Rel: Any = Literal[relations]
    Pair = create_model("PropertyColumn", property=(Prop, ...), column=(Col, ...))
    Ent = create_model("EntityDraft", class_name=(Cls, ...), columns=(list[Pair], ...))
    End = create_model("EndDraft", class_name=(Cls, ...), key_column=(Col, ...))
    RelM = create_model("RelationDraft", relation=(Rel, ...), source=(End, ...), target=(End, ...))
    Draft = create_model("MappingDraft", entities=(list[Ent], ...), relations=(list[RelM], ...))
    result = llm.ask(
        MAPPING_PROMPT,
        {
            "ontology": render_ontology(spec),
            "table": ds.table_name,
            "columns": ", ".join(ds.columns),
            "rows": "\n".join(row_text(r) for r in sample[:5]),
        },
        Draft,
        ontology_version=version,
    )
    data = result.value.model_dump()
    return DatasetMapping(
        entities=[
            EntityMapping(
                class_name=e["class_name"],
                columns={p["property"]: p["column"] for p in e["columns"]},
            )
            for e in data["entities"]
        ],
        relations=[
            RelationMapping.model_validate(r) for r in data["relations"] if r["relation"] != "NONE"
        ],
    )


# --------------------------------------------------------------------------- storage


class MappingError(Exception):
    pass


def save_draft(
    uow: UnitOfWork,
    ds: DatasetInfo,
    mapping: DatasetMapping,
    *,
    suggested_by: Literal["llm", "user"],
    actor: str,
) -> ColumnMapping:
    row = ColumnMapping(
        dataset_id=ds.id,
        dataset_name=ds.table_name,
        mapping=mapping.model_dump(),
        suggested_by=suggested_by,
    )
    uow.session.add(row)
    uow.session.flush()
    uow.audit(actor, "mapping.draft", "column_mapping", str(row.id), {"dataset": ds.id})
    return row


def approve_mapping(
    uow: UnitOfWork, mapping_id: int, spec: OntologySpec, ds: DatasetInfo, *, actor: str
) -> ColumnMapping:
    """Validate and approve; any previously approved mapping for the dataset is retired."""
    row = uow.session.get(ColumnMapping, mapping_id)
    if row is None or row.status != "draft":
        raise MappingError(f"mapping {mapping_id} is not a draft")
    errors = validate_mapping(DatasetMapping.model_validate(row.mapping), spec, ds)
    if errors:
        raise MappingError("; ".join(errors))
    for old in uow.session.scalars(
        select(ColumnMapping).where(
            ColumnMapping.dataset_id == ds.id, ColumnMapping.status == "approved"
        )
    ):
        old.status = "retired"
    row.status, row.approved_by, row.approved_at = "approved", actor, utcnow()
    uow.audit(actor, "mapping.approve", "column_mapping", str(row.id), {"dataset": ds.id})
    return row


def approved_mapping(uow: UnitOfWork, dataset_id: int) -> DatasetMapping | None:
    row = uow.session.scalars(
        select(ColumnMapping).where(
            ColumnMapping.dataset_id == dataset_id, ColumnMapping.status == "approved"
        )
    ).first()
    return DatasetMapping.model_validate(row.mapping) if row else None
