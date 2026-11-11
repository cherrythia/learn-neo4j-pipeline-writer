# ADR-0003: Provider-agnostic LLM client, Claude Sonnet by default

- Status: Accepted
- Date: 2026-11-12

## Context
The team wants Claude Sonnet now but the freedom to switch providers (for example to on-prem).

## Decision
All LLM calls go through an `LLMClient` protocol with structured output. The default adapter is Anthropic `claude-sonnet-5-5`, selected by `WRITER_LLM_PROVIDER` / `WRITER_LLM_MODEL`. A deterministic `FakeLLM` is used in tests and CI.

## Consequences
Switching provider is one adapter plus config. Provider-specific features are only used behind the interface.

## Update (issue #5)
Claude Sonnet 5.5 rejects forced `tool_choice` and non-default sampling parameters, so the
adapter uses structured outputs (`output_config.format`) and sends no temperature. Anthropic's
server-side fallback on safety refusals is enabled by default and can be turned off with
`WRITER_LLM_FALLBACKS=false`. See [`docs/llm.md`](../llm.md).
