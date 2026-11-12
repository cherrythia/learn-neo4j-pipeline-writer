"""Pipeline wiring for HTTP routes. Tests override ``get_pipeline`` with an in-memory one."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, Request

from writer_service.ingestion.superset import DatasetReader, SupersetClient
from writer_service.llm.base import LLMConfigError
from writer_service.llm.factory import build_structured_llm
from writer_service.ontology.neo4j_schema import GraphSchema
from writer_service.pipeline.orchestrator import WriterPipeline

log = logging.getLogger(__name__)
LLM_HINT = "configure WRITER_LLM_PROVIDER (and WRITER_ANTHROPIC_API_KEY for Claude)"
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


_DRIVER_LOCK = threading.Lock()


def get_neo4j_driver(request: Request) -> Any:
    """One Neo4j driver per app (lazy, race-free), shared by the pipeline and the ontology
    schema manager; closed on shutdown (see ``app.lifespan``). Needs no LLM."""
    driver = getattr(request.app.state, "neo4j_driver", None)
    if driver is not None:
        return driver
    with _DRIVER_LOCK:
        driver = getattr(request.app.state, "neo4j_driver", None)
        if driver is None:
            from neo4j import GraphDatabase

            s = request.app.state.settings
            driver = GraphDatabase.driver(
                s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value())
            )
            request.app.state.neo4j_driver = driver
        return driver


def _build_pipeline(request: Request) -> WriterPipeline:
    from writer_service.pipeline.graph_store import Neo4jGraph

    try:
        llm = build_structured_llm(request.app.state.settings)
    except LLMConfigError as exc:
        # Review, rollback and structured sync don't need an LLM; routes that do check
        # ``pipeline.llm`` and answer 503 themselves.
        log.warning("LLM not configured (%s); LLM-backed routes will answer 503", exc)
        llm = None
    return WriterPipeline(request.app.state.engine, Neo4jGraph(get_neo4j_driver(request)), llm)


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


def get_graph_schema(request: Request) -> GraphSchema:
    """Neo4j constraint management for ontology publish/rollback (shares the app's driver)."""
    from writer_service.ontology.neo4j_schema import Neo4jGraphSchema

    return Neo4jGraphSchema(get_neo4j_driver(request))


GraphSchemaDep = Annotated[GraphSchema, Depends(get_graph_schema)]
