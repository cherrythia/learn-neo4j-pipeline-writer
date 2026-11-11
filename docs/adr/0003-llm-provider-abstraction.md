# ADR-0003: Provider-agnostic LLM client, Claude Sonnet by default

- Status: Accepted
- Date: 2026-11-12

## Context
The team wants Claude Sonnet now but the freedom to switch providers (for example to on-prem).

## Decision
All LLM calls go through an `LLMClient` protocol with structured output. The default adapter is Anthropic `claude-sonnet-5-5`, selected by `WRITER_LLM_PROVIDER` / `WRITER_LLM_MODEL`. A deterministic `FakeLLM` is used in tests and CI.

## Consequences
Switching provider is one adapter plus config. Provider-specific features are only used behind the interface.
