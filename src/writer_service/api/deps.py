"""Pipeline wiring for HTTP routes. Tests override ``get_pipeline`` with an in-memory one."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from writer_service.llm.base import LLMConfigError
from writer_service.llm.factory import build_structured_llm
from writer_service.pipeline.orchestrator import WriterPipeline


def get_pipeline(request: Request) -> WriterPipeline:
    """Build (once per app) the production pipeline: governance engine + Neo4j + LLM."""
    pipeline: WriterPipeline | None = getattr(request.app.state, "pipeline", None)
    if pipeline is not None:
        return pipeline
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
    pipeline = WriterPipeline(request.app.state.engine, Neo4jGraph(driver), llm)
    request.app.state.pipeline = pipeline
    return pipeline


PipelineDep = Annotated[WriterPipeline, Depends(get_pipeline)]
