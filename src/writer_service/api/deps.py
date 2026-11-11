"""Pipeline wiring for HTTP routes. Tests override ``get_pipeline`` with an in-memory one."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from writer_service.ingestion.superset import DatasetReader, SupersetClient
from writer_service.llm.base import LLMConfigError
from writer_service.llm.factory import build_structured_llm
from writer_service.pipeline.orchestrator import WriterPipeline

_PIPELINE_LOCK = threading.Lock()


def get_pipeline(request: Request) -> WriterPipeline:
    """Build (once per app, race-free) the production pipeline: governance engine + Neo4j +
    LLM. The Neo4j driver is closed when the app shuts down (see ``app.lifespan``)."""
    pipeline: WriterPipeline | None = getattr(request.app.state, "pipeline", None)
    if pipeline is not None:
        return pipeline
    with _PIPELINE_LOCK:
        pipeline = getattr(request.app.state, "pipeline", None)
        if pipeline is None:
            pipeline = _build_pipeline(request)
            request.app.state.pipeline = pipeline
        return pipeline


def _build_pipeline(request: Request) -> WriterPipeline:
    from neo4j import GraphDatabase

    from writer_service.pipeline.graph_store import Neo4jGraph

    settings = request.app.state.settings
    try:
        llm = build_structured_llm(settings)
    except LLMConfigError as exc:
        raise HTTPException(503, str(exc)) from exc
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value())
    )
    request.app.state.neo4j_driver = driver
    return WriterPipeline(request.app.state.engine, Neo4jGraph(driver), llm)


PipelineDep = Annotated[WriterPipeline, Depends(get_pipeline)]


def get_superset(request: Request) -> Iterator[SupersetClient]:
    """One client per request, closed afterwards (no leaked connections)."""
    s = request.app.state.settings
    with SupersetClient(
        s.superset_url, s.superset_username, s.superset_password.get_secret_value()
    ) as client:
        yield client


def get_reader(request: Request) -> DatasetReader:
    reader: DatasetReader | None = getattr(request.app.state, "dataset_reader", None)
    if reader is None:
        from sqlalchemy import create_engine

        reader = DatasetReader(
            create_engine(request.app.state.settings.source_db_url, pool_pre_ping=True)
        )
        request.app.state.dataset_reader = reader
    return reader


SupersetDep = Annotated[SupersetClient, Depends(get_superset)]
ReaderDep = Annotated[DatasetReader, Depends(get_reader)]
