# LLM layer

How the pipeline talks to an LLM, and how it keeps answers repeatable.
[ADR-0003](adr/0003-llm-provider-abstraction.md), [ADR-0005](adr/0005-containing-llm-nondeterminism.md).

## Layers

```
pipeline code
   │  ask(prompt, variables, OutputModel, ontology_version, sample)
   ▼
StructuredLLM ── cache hit? ──► return cached, validated answer
   │ miss
   │  render versioned prompt + strict JSON schema from the pydantic model
   ▼
LLMClient (protocol)            ◄── swap providers here
   ├─ AnthropicLLM   Claude via the official SDK (default: claude-sonnet-5-5)
   ├─ FakeLLM        scripted answers for tests
   └─ ReplayOnlyLLM  recorded answers only; a miss is an error
   │
   ▼
validate with pydantic ── invalid? retry with the error fed back (WRITER_LLM_MAX_ATTEMPTS)
   │ valid
   ▼
cache.put(key, answer)  → return
```

| Module | Role |
|---|---|
| `llm/base.py` | `LLMClient` protocol, `LLMRequest` / `LLMResponse`, error types |
| `llm/prompts.py` | `PromptTemplate(name, version, system, user)` |
| `llm/schema.py` | `strict_json_schema(Model)`: closed objects, all properties required |
| `llm/structured.py` | `StructuredLLM.ask()`: cache, validate, retry |
| `llm/cache.py` | `InMemoryCache`, `FileCache` (one JSON file per key) |
| `llm/anthropic_client.py` | Claude adapter |
| `llm/fake.py` | `FakeLLM`, `ReplayOnlyLLM` |
| `llm/factory.py` | Build from settings; fails fast without an API key |

## Repeatability

An LLM is not bit-for-bit repeatable. Claude Sonnet 5.5 also **rejects** non-default
`temperature`/`top_p`, so "turn randomness down" is not available. Instead:

- **Structured output.** The request carries a JSON schema (`output_config.format`), and the
  answer is validated again with pydantic.
- **Versioned prompts.** Change the wording, bump `version`.
- **Cache key** = SHA-256 of model, prompt name + version, rendered system/user text, schema,
  ontology version and **sample index**. The same input therefore replays the same answer, and
  any change that could alter the answer misses the cache.
- **Sample index.** Self-consistency (issue #8) asks N times with `sample=0..N-1`. Each sample
  has its own key, so the N calls are real the first time and replay identically afterwards.
- Only **validated** answers are cached; a failure is never replayed.

## Claude specifics

- Structured outputs, not forced tool use: `tool_choice` `any`/`tool` returns 400 on Sonnet 5.5.
- `output_config.effort` from `WRITER_LLM_EFFORT` (default `high`, Sonnet 5.5's default).
- **Server-side fallback is on by default** (`WRITER_LLM_FALLBACKS=true`): if Claude declines a
  request on safety grounds, the API re-runs it on a suitable model in the same call. The
  adapter reads only the output after the last `fallback` content block, and records the
  serving model from the `fallback_message` entry in `usage.iterations` (stored with each
  cached answer). Set it to
  `false` to get a plain `LLMRefusalError` instead.
- `stop_reason: refusal` → `LLMRefusalError` (not retried); `max_tokens` or non-JSON →
  `LLMOutputError`, which `StructuredLLM` retries with the error fed back, like a schema
  mismatch. `LLMReplayMissError` (replay mode) is never retried.

## Switching provider

1. Write a class with a `model` property and `complete_json(request) -> LLMResponse`.
2. Add it to `llm/factory.py` and to `LLMProvider` in `config.py`.
3. Set `WRITER_LLM_PROVIDER` / `WRITER_LLM_MODEL`.

Nothing else changes: prompts, caching, validation and the pipeline are provider-neutral.

## Testing

- Unit tests use `FakeLLM`; the Claude adapter is tested through the real `anthropic` SDK
  over a mocked HTTP transport (`httpx2.MockTransport`). CI never calls a real model.
- Recorded fixtures: a `FileCache` directory can be committed and replayed with
  `ReplayOnlyLLM`.
- Live check (optional, costs a few cents):
  `WRITER_TEST_ANTHROPIC_API_KEY=sk-... uv run pytest -m live`.
