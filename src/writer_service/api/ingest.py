"""``POST /ingest/documents``: upload a PDF or Excel file and run it through the pipeline."""

from __future__ import annotations

from pathlib import PurePath

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from writer_service.api.deps import LLM_HINT, PipelineDep, ReaderDep, SupersetDep
from writer_service.auth.deps import SettingsDep, require_api_key
from writer_service.governance.uow import UnitOfWork
from writer_service.ingestion.documents import DocumentExtractor, pdf_chunks, xlsx_chunks
from writer_service.ingestion.superset import (
    DatasetNotFound,
    SourceReadError,
    SupersetError,
    approved_mapping,
    rows_to_candidates,
)
from writer_service.ontology.service import spec_of
from writer_service.pipeline.orchestrator import RunSummary, SourceBatch, SourceText

router = APIRouter(prefix="/ingest", tags=["ingest"], dependencies=[Depends(require_api_key)])

PARSERS = {".pdf": ("pdf", pdf_chunks), ".xlsx": ("excel", xlsx_chunks)}


class RunOut(BaseModel):
    run_id: str
    status: str
    counts: dict[str, int]
    error: str | None = None


def _out(summary: RunSummary) -> RunOut:
    # Internal error text (driver messages, SQL) stays in the run record, not the response.
    error = (
        f"run failed; details are stored with run {summary.run_id}"
        if summary.status == "failed"
        else None
    )
    return RunOut(run_id=summary.run_id, status=summary.status, counts=summary.counts, error=error)


@router.post("/documents", response_model=RunOut)
def ingest_document(file: UploadFile, pipeline: PipelineDep, settings: SettingsDep) -> RunOut:
    """Synchronous: returns when the run has finished (fine for demo-sized documents)."""
    name = PurePath(file.filename or "upload").name  # never trust a client path
    suffix = PurePath(name).suffix.lower()
    if suffix not in PARSERS:
        raise HTTPException(415, "only .pdf and .xlsx files are supported")
    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"file larger than {settings.max_upload_mb} MB")
    if pipeline.llm is None:
        raise HTTPException(503, f"document ingestion needs an LLM; {LLM_HINT}")
    source_type, parse = PARSERS[suffix]
    try:
        chunks = parse(data, source_ref=name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not chunks:
        raise HTTPException(422, f"{name} contains no text")
    with UnitOfWork(pipeline.engine) as uow:
        published = uow.ontology_versions.published()
        if published is None:
            raise HTTPException(409, "no published ontology; run `writer-ontology bootstrap`")
        spec, version = spec_of(published), published.version
    extractor = DocumentExtractor(pipeline.llm, spec, version)
    batch = SourceBatch(
        source_type,  # type: ignore[arg-type]
        name,
        [SourceText(c.source_ref, c.locator, c.text) for c in chunks],
        extract=lambda sample: extractor.extract(chunks, sample),
    )
    summary = pipeline.ingest(batch, actor="ingest-api")
    return _out(summary)


@router.post("/superset/{dataset_id}", response_model=RunOut)
def ingest_superset(
    dataset_id: int,
    pipeline: PipelineDep,
    superset: SupersetDep,
    reader: ReaderDep,
    settings: SettingsDep,
) -> RunOut:
    """Sync a Superset dataset through its admin-approved column mapping."""
    with UnitOfWork(pipeline.engine) as uow:
        mapping = approved_mapping(uow, dataset_id)
    if mapping is None:
        raise HTTPException(409, f"dataset {dataset_id} has no approved column mapping")
    try:
        ds = superset.dataset(dataset_id)
    except DatasetNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except SupersetError as exc:
        raise HTTPException(502, str(exc)) from exc
    try:
        rows = list(reader.rows(ds, limit=settings.max_rows_per_sync))
    except SourceReadError as exc:  # message names the dataset only, never SQL or the driver
        raise HTTPException(502, str(exc)) from exc
    candidates, texts = rows_to_candidates(mapping, ds, rows)
    batch = SourceBatch("superset", ds.ref, [SourceText(*t) for t in texts], candidates=candidates)
    summary = pipeline.ingest(batch, actor="superset-sync")
    return _out(summary)
