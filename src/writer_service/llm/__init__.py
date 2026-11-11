"""Provider-agnostic LLM access (ADR-0003, ADR-0005).

Pipeline code talks to :class:`~writer_service.llm.structured.StructuredLLM`, which renders a
versioned prompt, asks any :class:`~writer_service.llm.base.LLMClient` for JSON matching a
pydantic model, validates it, retries with feedback, and caches the validated result.
"""
